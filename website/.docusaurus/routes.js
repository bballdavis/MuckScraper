import React from 'react';
import ComponentCreator from '@docusaurus/ComponentCreator';

export default [
  {
    path: '/MuckScraper/',
    component: ComponentCreator('/MuckScraper/', '655'),
    routes: [
      {
        path: '/MuckScraper/',
        component: ComponentCreator('/MuckScraper/', '986'),
        routes: [
          {
            path: '/MuckScraper/',
            component: ComponentCreator('/MuckScraper/', '170'),
            routes: [
              {
                path: '/MuckScraper/API',
                component: ComponentCreator('/MuckScraper/API', '282'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/architecture',
                component: ComponentCreator('/MuckScraper/architecture', 'c4f'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/configuration',
                component: ComponentCreator('/MuckScraper/configuration', '916'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/features',
                component: ComponentCreator('/MuckScraper/features', '928'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/getting-started',
                component: ComponentCreator('/MuckScraper/getting-started', '0c5'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/MCP',
                component: ComponentCreator('/MuckScraper/MCP', '863'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/OIDC',
                component: ComponentCreator('/MuckScraper/OIDC', 'de1'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/security',
                component: ComponentCreator('/MuckScraper/security', 'e22'),
                exact: true,
                sidebar: "docs"
              },
              {
                path: '/MuckScraper/',
                component: ComponentCreator('/MuckScraper/', 'ccf'),
                exact: true,
                sidebar: "docs"
              }
            ]
          }
        ]
      }
    ]
  },
  {
    path: '*',
    component: ComponentCreator('*'),
  },
];
