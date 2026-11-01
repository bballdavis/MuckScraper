// The docs site is built from the docs/ folder at the repository root, so the
// published pages are always the ones in the branch that was built.
//
// DOCS_URL and DOCS_BASE_URL override where the site is published, for example
// when it is served from a custom domain.

const siteUrl = process.env.DOCS_URL || 'https://bballdavis.github.io';
const baseUrl = process.env.DOCS_BASE_URL || '/MuckScraper/';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'MuckScraper',
  tagline: 'A self-hosted news aggregator with multi-source grouping and local LLM analysis',
  favicon: 'img/favicon.ico',

  url: siteUrl,
  baseUrl,
  organizationName: 'bballdavis',
  projectName: 'MuckScraper',

  // A broken link is a broken build: the docs are the reference, so they must not rot.
  onBrokenLinks: 'throw',
  onBrokenMarkdownLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          routeBasePath: '/',
          sidebarPath: require.resolve('./sidebars.js'),
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      },
    ],
  ],

  themeConfig: {
    image: 'img/logo.png',
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'MuckScraper',
      logo: {
        alt: 'MuckScraper',
        src: 'img/logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/bballdavis/MuckScraper',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      copyright: `MIT licensed. Built with Docusaurus.`,
    },
    prism: {
      additionalLanguages: ['bash', 'docker', 'python', 'json'],
    },
  },
};

module.exports = config;
