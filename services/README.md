# Vulnerable Services

No vulnerable application is bundled in this scaffold.

Future services can live under this directory, for example:

```text
services/
└── example-vuln/
    ├── Dockerfile
    └── ...
```

Build the service image yourself, then pass that image to the infrastructure:

```bash
docker build -t sandcastle/example-vuln ./services/example-vuln
VULN_IMAGE=sandcastle/example-vuln ./scripts/start.sh
```

The generated Compose file will run the same image once per team as
`team<N>-vuln`.
