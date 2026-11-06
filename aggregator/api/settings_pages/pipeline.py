"""Admin › Pipeline: what the scheduler is doing (Run now, Pause, the job
queue, one-off jobs) and the hours the pipeline runs. Same data and rules as
blueprints/admin/tasks_page.py and config_crud.py."""
from flask import g

from aggregator import db, jobs, scheduler_tasks
from aggregator.models import PipelineSchedule

from . import page
from . import schema as s
from .schema import FieldError

TONES = {"ok": "ok", "run": "ok", "bad": "bad", "warn": "warn", "off": None}
# The one-off jobs the app can start: those the job worker runs. The rest
# (Wake Ollama, the AllSides sync, merging outlets) run in the web process.
ONE_OFF = ("ollama_catchup", "scrape_all_missing", "reclassify_articles", "audit_scrapes", "force_resummarize")


def _context():
    from aggregator.blueprints.admin.tasks_page import _context as tasks_context
    return tasks_context()


def _one_off_jobs():
    from aggregator.blueprints.admin.tools import OVERVIEW_JOBS
    by_bulk = {j.get("bulk"): j for j in OVERVIEW_JOBS if j.get("bulk")}
    return [(key, by_bulk[key]) for key in ONE_OFF if key in by_bulk]


@page("tasks", "Tasks", "pipeline", "tasks",
      detail=lambda: "Running" if scheduler_tasks.state().get("alive") else "Scheduler down")
def tasks(_=None):
    c = _context()
    sch = c["scheduler"]
    sections = [s.section("scheduler", "Scheduler", facts=[
        s.fact("Scheduler", "Running" if sch["alive"] else "Not running", "ok" if sch["alive"] else "bad"),
        s.fact("Last heartbeat", f"{sch['heartbeat']} ago" if sch["heartbeat"] else None),
        s.fact("Up since", sch["since"]),
        s.fact("Job worker", "Running" if sch["worker_alive"] else "Not running",
               None if sch["worker_alive"] else "warn"),
        s.fact("Queued jobs", c["queue_counts"].get("queued", 0)),
    ])]
    running = c["running"]
    if running:
        sections.append(s.section("running", "Running now", facts=[
            s.fact("Run", running["kind"]),
            s.fact("Started", running["started"]),
            s.fact("Step", running["step"], "warn" if running["slow"] else None),
            s.fact("On this step", running["step_elapsed"]),
        ] + [s.fact(st["label"], {"run": "Running", "done": st["time"] or "Done", "todo": "Waiting",
                                  "skip": "Skipped"}[st["state"]]) for st in running["stages"]]))
    sections.append(s.section("recurring", "Recurring", items=[
        s.item(r["id"], r["name"], subtitle=f"{r['every']} · next {r['next']}",
               badge=s.badge("Run asked" if r["requested"] else r["label"], TONES.get(r["state"])),
               page=f"tasks/{r['id']}") for r in c["recurring"]
    ]))
    sections.append(s.section("queue", "Job queue", items=[
        s.item(j["id"], j["label"], subtitle=j["when"], detail=None,
               badge=s.badge({"running": "Running", "queued": "Waiting", "error": "Failed"}.get(j["status"]),
                             {"running": "ok", "error": "bad"}.get(j["status"])),
               remove="cancel" if j["status"] == "queued" else None, remove_label="Cancel")
        for j in c["queue"]
    ], empty="Nothing waiting.", footer="Swipe a waiting job to cancel it."))
    sections.append(s.section("jobs", "One-off jobs", actions=[
        s.action(key, job["name"], role="destructive" if job["risk"] == "danger" else None,
                 confirm=job.get("confirm") or f"{job['name']}? {job['desc']}.")
        for key, job in _one_off_jobs()
    ], footer="Each runs on the scheduler's job worker. Wake Ollama, the AllSides sync and merging outlets "
              "are on the web."))
    if c["history"]:
        sections.append(s.section("history", "Recent runs", items=[
            s.item(i, h["started"], subtitle=" · ".join(x for x in (h["kind"], h["took"],
                                                                    f"{h['stored']} stored" if h["stored"] is not None
                                                                    else None) if x),
                   detail=h["slowest"].removeprefix("Slowest: ") if h["slowest"] else None,
                   badge=s.badge("Failed", "bad") if h["status"] == "error" else None)
            for i, h in enumerate(c["history"])
        ]))
    return s.page("tasks", "Tasks", sections)


@tasks.action("cancel")
def cancel_job(_, values):
    job_id = values.get("id")
    if not isinstance(job_id, str) or not job_id.isdigit():
        raise FieldError("Pick a job.")
    if not jobs.cancel(int(job_id)):
        raise FieldError("That job already started or finished.")
    return {"message": "Job cancelled."}


def _one_off(key):
    def run(_, values):
        from aggregator.blueprints.admin._tasks import _start_bulk_task
        started, _status = _start_bulk_task(key)
        name = dict(_one_off_jobs())[key]["name"]
        return {"message": f"{name}: queued." if started else f"{name} is already running."}
    return run


for _key in ONE_OFF:
    tasks.action(_key)(_one_off(_key))


def _task_row(arg):
    if arg not in scheduler_tasks.TASKS:
        raise LookupError(arg)
    row = next((r for r in _context()["recurring"] if r["id"] == arg), None)
    if row is None:
        raise LookupError(arg)
    return row


@page("tasks/*", "Task", "pipeline", "tasks", listed=False)
def task_page(arg):
    r = _task_row(arg)
    facts = [
        s.fact("State", r["label"], TONES.get(r["state"])),
        s.fact("Runs", r["every"]),
        s.fact("Next", r["next"]),
        s.fact("Last run", r["last"]),
        s.fact("Took", r["took"]),
        s.fact("Result", r["summary"]),
    ]
    if r["error"]:
        facts.append(s.fact("Error", r["error"][:400], "bad"))
    actions = [s.action("run", "Asked to run" if r["requested"] else "Run now")]
    if arg not in scheduler_tasks.UNPAUSABLE:
        actions.append(s.action("resume" if r["paused"] else "pause", "Resume" if r["paused"] else "Pause"))
    return s.page(f"tasks/{arg}", r["name"], [
        s.section("task", facts=facts, footer=r["desc"] + ".", actions=actions),
    ])


@task_page.action("run")
def run_task(arg, values):
    _task_row(arg)
    scheduler_tasks.request_run(arg, g.user.username)
    return {"message": "Asked the scheduler to run it. It starts within a few seconds."}


@task_page.action("pause")
def pause_task(arg, values):
    _task_row(arg)
    scheduler_tasks.set_paused(arg, True)
    return {"message": "Paused."}


@task_page.action("resume")
def resume_task(arg, values):
    _task_row(arg)
    scheduler_tasks.set_paused(arg, False)
    return {"message": "Resumed."}


# --- Run schedule ----------------------------------------------------------------------

HOURS = [(h, f"{(h % 12) or 12}:00 {'AM' if h < 12 else 'PM'}") for h in range(24)]


def _hour(h):
    return HOURS[h][1]


def _changed():
    from aggregator.blueprints.admin.config_crud import _mark_pipeline_schedule_changed
    _mark_pipeline_schedule_changed()


@page("schedule", "Run schedule", "pipeline", "schedule",
      detail=lambda: str(PipelineSchedule.query.filter_by(is_active=True).count()))
def schedule(_=None):
    from aggregator.blueprints.admin.config_crud import pipeline_schedule_restart_needed
    entries = PipelineSchedule.query.order_by(PipelineSchedule.hour.asc()).all()
    taken = {e.hour for e in entries}
    return s.page("schedule", "Run schedule", [
        s.section("runs", items=[
            s.item(e.id, _hour(e.hour), subtitle="Full pipeline" if e.run_full_pipeline else "Fetch only",
                   page=f"schedule/{e.id}", active=e.is_active) for e in entries
        ], empty="Nothing scheduled.", actions=[s.action("add", "Add a run", fields=[
            s.choice("hour", "At", next((h for h in range(24) if h not in taken), 0), HOURS),
            s.toggle("run_full_pipeline", "Full pipeline", True,
                     help="Fetch, enrich, headlines and publish the edition. Off fetches only."),
        ])], footer="Eastern time. A full run publishes an edition; a fetch-only run brings in new articles."),
    ], notice="The scheduler hasn't picked up the last change yet. It may be down: check Tasks."
       if pipeline_schedule_restart_needed() else None, tone="warn")


@schedule.action("add")
def add_run(_, values):
    hour = int(s.get_choice(values, "hour", range(24)))
    if PipelineSchedule.query.filter_by(hour=hour).first():
        raise FieldError(f"There's already a run at {_hour(hour)}.", "hour")
    full = s.get_bool(values, "run_full_pipeline") if "run_full_pipeline" in values else True
    db.session.add(PipelineSchedule(hour=hour, run_full_pipeline=full, is_active=True))
    db.session.commit()
    _changed()
    return {"message": f"Added a run at {_hour(hour)}."}


def _entry(arg):
    entry = PipelineSchedule.query.get(int(arg)) if arg and arg.isdigit() else None
    if entry is None:
        raise LookupError(arg)
    return entry


@page("schedule/*", "Run", "pipeline", "schedule", listed=False)
def run_page(arg):
    e = _entry(arg)
    return s.page(f"schedule/{e.id}", _hour(e.hour), [
        s.section("run", fields=[
            s.toggle("active", "On", e.is_active),
            s.choice("hour", "At", e.hour, HOURS),
            s.toggle("run_full_pipeline", "Full pipeline", e.run_full_pipeline),
        ], save="auto"),
        s.section("remove", actions=[s.action("delete", "Delete this run", role="destructive",
                                              confirm=f"Stop running at {_hour(e.hour)}?")]),
    ])


@run_page.save
def save_run(arg, values, section):
    e = _entry(arg)
    if "hour" in values:
        hour = int(s.get_choice(values, "hour", range(24)))
        if PipelineSchedule.query.filter(PipelineSchedule.hour == hour, PipelineSchedule.id != e.id).first():
            raise FieldError(f"There's already a run at {_hour(hour)}.", "hour")
        e.hour = hour
    if "run_full_pipeline" in values:
        e.run_full_pipeline = s.get_bool(values, "run_full_pipeline")
    if "active" in values:
        e.is_active = s.get_bool(values, "active")
    db.session.commit()
    _changed()
    return None


@run_page.action("delete")
def delete_run(arg, values):
    e = _entry(arg)
    db.session.delete(e)
    db.session.commit()
    _changed()
    return {"message": "Run deleted.", "close": True}
