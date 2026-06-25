# Webex Calling SIP Test Framework Prototype

`wxcalls` is an internal Python prototype for testing Webex Calling call flows with
pre-provisioned generic third-party SIP phone credentials.

The first version is Mac-first and scenario-oriented:

- register one or more SIP clients through a PJSUA2 backend;
- place and handle calls;
- answer, reject, hold, resume, consult, and attended-transfer calls;
- play prerecorded WAV or runtime TTS audio;
- record call audio and assert deterministic tone markers;
- run video smoke probes that skip cleanly when unsupported by the tenant or endpoint.

## Install for development

```bash
uv sync --extra dev
```

PJSUA2 is a native dependency and is not declared as a PyPI dependency. The framework imports
`pjsua2` only when the live backend is used. The fake backend and unit tests run without it.

## Configuration

Copy `config.example.yml` to a local config file and create a local `.env` file containing the
SIP credential values referenced by the config.
For Webex outbound proxies that publish only SIP SRV records, set `dns_nameservers` and use a
`sips:` proxy URI so PJSIP can resolve `_sips._tcp` targets.

```bash
cp config.example.yml config.local.yml
cat > .env <<'EOF'
WX_SIP_ALICE_USER=alice@example.invalid
WX_SIP_ALICE_PASSWORD=replace-me
WX_SIP_BOB_USER=bob@example.invalid
WX_SIP_BOB_PASSWORD=replace-me
EOF
```

Run preflight:

```bash
wxcalls preflight -c config.local.yml
```

Run YAML scenarios through pytest:

```bash
pytest --wxcalls-config config.local.yml --wxcalls-backend pjsua2 --scenario scenarios/*.yml
```

Use `--wxcalls-backend fake` for local parser/orchestrator smoke runs without Webex credentials.

## Notes

This is an internal prototype. PJSIP/PJSUA2 build and licensing requirements should be reviewed
before packaging the framework for wider internal use or external distribution.
