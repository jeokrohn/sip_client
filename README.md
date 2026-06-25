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

## Call scenarios

Use `wait_state` to assert call signaling state and `wait_media` to assert that audio media is active before playback:

```yaml
steps:
  - action: call
    client: device1
    target: "7109"
    save_as: outbound
  - action: wait_state
    call: outbound
    state: connected
  - action: wait_media
    call: outbound
```

Numeric targets such as `"7109"` are treated as extensions and dialed against the calling client's registrar host.

## Registration watch scenarios

The `register` step can keep one or more clients registered before moving to the next step.

```yaml
steps:
  - action: register
    client: alice
    timeout: 30
    stay_registered: true
```

With `stay_registered: true`, the framework waits for twice the accepted `expires` interval
reported by the successful `200 REGISTER` response and requires at least one successful
re-registration refresh before the step completes.

Use an explicit duration when you want a shorter or longer soak:

```yaml
steps:
  - action: register
    clients: [alice, bob]
    stay_registered_for: 120
    require_reregistration: true
    min_reregistrations: 1
```

`stay_registered_for` is in seconds. For explicit durations, `require_reregistration` defaults to
`false`; set it to `true` when the scenario should fail unless a refresh is observed.

## Notes

This is an internal prototype. PJSIP/PJSUA2 build and licensing requirements should be reviewed
before packaging the framework for wider internal use or external distribution.
