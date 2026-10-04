# Webex Calling SIP Test Framework Prototype

`wxcalls` is an open-source Python prototype for testing Webex Calling call flows with
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

PJSUA2 is a native dependency and is not declared as a package dependency. The framework imports
`pjsua2` only when the live backend is used. Install locally built PJSUA2 bindings into the active
environment before live backend runs. The fake backend and unit tests run without it.

## Configuration

Copy `config.example.yml` to a local config file and create a local `.env` file containing the
SIP credential values referenced by the config.
For Webex outbound proxies that publish only SIP SRV records, set `dns_nameservers` and use a
`sips:` proxy URI so PJSIP can resolve `_sips._tcp` targets. If `dns_nameservers` is omitted or
empty on macOS, `wxcalls` falls back to the nameservers reported by `scutil --dns`.

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

Scenario runs print concise progress lines for call lifecycle events, including when calls are initiated, received,
established, and ended.

Run YAML scenario through the CLI:

```bash
wxcalls run -c config.local.yml scenarios/call-behaviors.yml
```

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
Set `extension` on a client when the framework should know that a configured SIP identity owns a numeric extension:

```yaml
clients:
  - name: bob
    id_uri: "sip:bob@example.webexcalling.invalid"
    registrar_uri: "sip:example.webexcalling.invalid"
    username_env: WX_SIP_BOB_USER
    password_env: WX_SIP_BOB_PASSWORD
    extension: "7108"
```

The live backend still dials `target: "7108"` as `sip:7108@<caller-registrar-host>`. The extension ownership field
lets the fake backend and local smoke tests map that dialed extension URI back to the configured client.

To keep scenarios readable while still dialing a configured client's extension, use the client name as `target` and
set `use_target_extension: true`:

```yaml
steps:
  - action: call
    client: alice
    target: bob
    use_target_extension: true
    save_as: alice_to_bob
```

This dials Bob's configured `extension` as `sip:<extension>@<alice-registrar-host>` instead of dialing Bob's `id_uri`.

Use `parallel` when two or more clients need to act independently during the same wall-clock interval:

```yaml
steps:
  - action: parallel
    branches:
      caller:
        - action: call
          client: alice
          target: bob
          save_as: alice_to_bob
        - action: wait_state
          call: alice_to_bob
          state: connected
      callee:
        - action: expect_incoming
          client: bob
          save_as: bob_incoming
        - action: answer
          call: bob_incoming
```

Use endpoint `behaviors` when one side of a scenario should react to events independently of the explicit step flow.
The step DSL remains available and is still the best fit for straightforward scripted scenarios:

```yaml
behaviors:
  auto_answer:
    initial_state: idle
    states:
      idle:
        on:
          call_received:
            save_call_as: inbound
            actions:
              - action: answer
                call: inbound
            next_state: connected

      connected:
        on:
          call_state:
            state: disconnected
            next_state: idle

endpoints:
  bob:
    client: bob
    behavior: auto_answer

steps:
  - action: register
    clients: [alice, bob]
  - action: call
    client: alice
    target: bob
    save_as: alice_to_bob
  - action: wait_state
    call: alice_to_bob
    state: connected
  - action: wait_media
    call: bob.inbound
```

Behavior call aliases are scoped by endpoint. In the example above, Bob's behavior can refer to `inbound`, while
scripted steps refer to the same call as `bob.inbound`. Behaviors support entry actions, `registered`,
`scenario_trigger`, `call_received`, `call_state`, `media_active`, and `timer_expired` events. Behavior actions can
reuse most scenario actions, plus behavior-local `start_timer` and `cancel_timer` actions. Use `wait_behavior_state`
plus `trigger_behavior` when scripted steps need to wait for several endpoint personas to become ready before one of
them starts reactive work.

See [docs/scenario-yaml.md](docs/scenario-yaml.md) for the complete scenario YAML reference.

For media-marker checks, prefer `record_during_playback` over a separate `play_tts` followed by `record`.
The action starts recording first, waits for a short pre-roll, plays the media, waits for post-roll, and verifies
the marker when `marker` is set:

```yaml
steps:
  - action: record_during_playback
    playback_call: alice_to_bob
    recording_call: bob_incoming
    text: "Webex Calling test marker."
    marker: marker-basic-audio
    save_as: bob_recording
    pre_roll: 0.25
    post_roll: 0.5
```

The live PJSUA2 backend requires SRTP on each account. Webex/BroadWorks inbound calls can offer `RTP/SAVP`
with SDES `a=crypto`, while still using `sip:` URIs over TLS. Client configs must use TLS transport, a `sips:`
proxy, or `;transport=tls`; plain RTP endpoints are rejected by design. SRTP offers are constrained to SDES
`AES_CM_128_HMAC_SHA1_80` to match Webex/BroadWorks answers. Voice scenarios offer one audio stream and disable
PJSUA2 text media negotiation.

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

## License

This project is distributed under the GNU General Public License, version 2 or later
(`GPL-2.0-or-later`). The live backend uses PJSIP/PJSUA2 on its GPL-compatible open-source path.
Do not redistribute a non-GPL-compatible combined build unless you have an appropriate commercial
PJSIP license.
