from aggregator import create_app, db
from aggregator.models import Outlet, Article
from news_fetcher.fetch_and_store_articles import normalize_source_name

app = create_app()

def merge_outlets():
    with app.app_context():
        outlets = Outlet.query.all()
        print(f"Total outlets before merging: {len(outlets)}")

        # Keep track of primary outlets
        # name -> Outlet object
        primaries = {}
        
        merged_count = 0
        
        for outlet in outlets:
            standard_name = normalize_source_name(outlet.name)
            
            if standard_name not in primaries:
                # First time seeing this standardized name
                # Check if an outlet with this name already exists in DB
                existing = Outlet.query.filter_by(name=standard_name).first()
                if existing:
                    primaries[standard_name] = existing
                else:
                    # Rename current outlet to standard name and make it primary
                    print(f"Standardizing: '{outlet.name}' -> '{standard_name}'")
                    outlet.name = standard_name
                    primaries[standard_name] = outlet
                    db.session.flush()
            
            primary = primaries[standard_name]
            
            if outlet.id != primary.id:
                # Merge articles
                print(f"Merging '{outlet.name}' (id:{outlet.id}) into '{primary.name}' (id:{primary.id})")
                for article in outlet.articles:
                    article.outlet_id = primary.id
                
                # Delete the redundant outlet
                db.session.delete(outlet)
                merged_count += 1

        db.session.commit()
        print(f"Merged {merged_count} redundant outlets.")
        
        # Second pass: ensure all primary outlets have names correctly set (in case of overlap)
        final_outlets = Outlet.query.all()
        for o in final_outlets:
            o.name = normalize_source_name(o.name)
        db.session.commit()
        print(f"Final outlet count: {len(final_outlets)}")

if __name__ == "__main__":
    merge_outlets()
