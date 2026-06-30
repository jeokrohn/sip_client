# Scenario YAML Reference

This document describes the scenario YAML files consumed by `wxcalls.scenario`.
It covers call-flow steps and optional endpoint behaviors. Lab configuration YAML
such as `config.example.yml` is separate and is not covered here.

## Root Document

Scenario files are YAML mappings.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | No | string | Human-readable scenario name. Defaults to the file stem when loaded from disk, or `scenario` when parsed from an in-memory object. |
| `steps` | Yes | non-empty list | Explicit scenario step sequence. Steps run in order unless a `parallel` step is used. |
| `behaviors` | No | mapping | Reusable endpoint behavior definitions. |
| `endpoints` | No | mapping | Assigns configured lab clients to behavior definitions. |

Example:

```yaml
name: basic-call

steps:
  - action: register
    clients: [alice, bob]
```

## Step Actions

Every step is a mapping with an `action` field. The parser also accepts `type`
as an alias for `action`.

Common numeric fields:

| Field | Constraint |
| --- | --- |
| `timeout` | Positive number when present. |
| `seconds` | Positive number when present. |
| `pre_roll` | Non-negative number when present. |
| `post_roll` | Non-negative number when present. |

### `register`

Registers one or more SIP clients.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `client` | Conditional | None | Single configured client name. Required when `clients` is absent. |
| `clients` | Conditional | None | List of configured client names. Required when `client` is absent. |
| `timeout` | No | `30.0` | Maximum registration wait in seconds. |
| `stay_registered` | No | `false` | Keep registration alive before moving to the next step. |
| `stay_registered_for` | No | Derived | Explicit duration to keep registration alive. |
| `require_reregistration` | No | `true` with derived duration, otherwise `false` | Whether a refresh must be observed. |
| `min_reregistrations` | No | `1` | Minimum refresh count when refreshes are required. |

### `call`

Places an outbound call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `client` | Yes | None | Configured client placing the call. |
| `target` | Yes | None | Target name, client name, SIP URI, or numeric extension. |
| `save_as` | No | `call` | Call alias saved in the scenario call map. |
| `timeout` | No | `30.0` | Maximum call setup wait in seconds. |
| `video` | No | `false` | Whether to offer video media. |
| `use_target_extension` | No | `false` | When `target` names a configured client, dial that client's `extension` rather than `id_uri`. |
| `reuse_alias` | No | `false` | Behavior actions only. Allows a disconnected existing behavior-owned `save_as` alias to be replaced for looping call flows. |

Behavior action default: inside endpoint behaviors, `client` may be omitted and
defaults to the endpoint's assigned client.

### `expect_incoming`

Waits for an incoming call on a client.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `client` | Yes | None | Configured client receiving the call. |
| `save_as` | No | `<client>_incoming` | Call alias saved in the scenario call map. |
| `timeout` | No | `30.0` | Maximum wait in seconds. |
| `from_uri` | No | None | Expected remote URI. |

Do not use `expect_incoming` for a client that also has an endpoint behavior
listening for `call_received`.

### `answer`

Answers an incoming call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `status_code` | No | `200` | SIP status code used to answer. |

### `reject`

Rejects an incoming call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `status_code` | No | `486` | SIP status code used to reject. |

### `wait_state`

Waits for a call to reach a backend-neutral state.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. The alias must already exist. |
| `state` | Yes | None | Expected call state, such as `connected`, `held`, or `disconnected`. |
| `timeout` | No | `30.0` | Maximum wait in seconds. |

### `wait_media`

Waits until audio media is active for a call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `timeout` | No | `30.0` | Maximum wait in seconds. |

### `wait_behavior_state`

Waits for an endpoint behavior to reach a state. Use this to synchronize
explicit steps with behavior-owned registration, timers, and call setup.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | Yes | None | Endpoint behavior instance name from the `endpoints` mapping. |
| `state` | Yes | None | Behavior state name. |
| `timeout` | No | `30.0` | Maximum wait in seconds. |

### `trigger_behavior`

Emits a named scenario trigger into one endpoint behavior. Use this after
`wait_behavior_state` steps to create an explicit readiness barrier before a
behavior starts a call or other reactive work.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | Yes | None | Endpoint behavior instance name from the `endpoints` mapping. |
| `name` | Yes | None | Trigger name matched by a `scenario_trigger` behavior rule. |

### `play_tts`

Generates TTS audio and plays it into a call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `text` | Yes | None | Speech text. |
| `marker` | No | None | Marker identifier appended as a deterministic tone. |
| `voice` | No | None | macOS voice name for TTS. |

### `play_wav`

Prepares and plays an existing WAV file into a call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `path` | Yes | None | Source WAV path. |
| `marker` | No | None | Marker identifier appended as a deterministic tone. |

### `record`

Records audio from a call into a WAV artifact.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |
| `save_as` | No | `recording` | Recording alias. |
| `seconds` | No | `3.0` | Recording duration. |

### `assert_marker`

Asserts that a marker tone exists in a recording.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `recording` | Yes | None | Recording alias. |
| `marker` | Yes | None | Marker identifier. |

### `hold`

Places a call on hold.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |

### `resume`

Resumes a held call.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Yes | None | Call alias. |

### `consult_call`

Places a consult call. It uses the same fields as `call`, but defaults
`save_as` to `consult_call`.

Behavior action default: inside endpoint behaviors, `client` may be omitted and
defaults to the endpoint's assigned client.

### `attended_transfer`

Completes an attended transfer.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `primary_call` | Yes | None | Original call alias. |
| `consult_call` | Yes | None | Consult call alias. |

### `hangup`

Hangs up one or more calls.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `call` | Conditional | None | Single call alias. Required when `calls` is absent. |
| `calls` | Conditional | None | List of call aliases. Required when `call` is absent. |

### `video_smoke`

Runs a video smoke probe.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `client` | Yes | None | Configured client placing the video probe. |
| `target` | Yes | None | Target name, SIP URI, or numeric extension. |
| `timeout` | No | `30.0` | Maximum call setup wait in seconds. |

### `parallel`

Runs named branches concurrently.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `branches` | Yes | non-empty mapping | Maps branch names to non-empty step lists. |

Example:

```yaml
steps:
  - action: parallel
    branches:
      caller:
        - action: call
          client: alice
          target: bob
          save_as: alice_to_bob
      callee:
        - action: expect_incoming
          client: bob
          save_as: bob_incoming
```

### `record_during_playback`

Records one call while playing media into another call. Exactly one of `text`
or `path` must be provided.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `playback_call` | Yes | None | Call alias receiving playback. |
| `recording_call` | Yes | None | Call alias being recorded. |
| `text` | Conditional | None | TTS text. Required when `path` is absent. |
| `path` | Conditional | None | Source WAV path. Required when `text` is absent. |
| `marker` | No | None | Marker identifier appended to playback media. |
| `voice` | No | None | macOS voice name for TTS. |
| `save_as` | No | `recording` | Recording alias. |
| `seconds` | No | Auto | Explicit recording duration. |
| `pre_roll` | No | `0.25` | Delay after recording starts and before playback starts. |
| `post_roll` | No | `0.5` | Delay after playback ends and before recording ends. |
| `assert_marker` | No | `true` when `marker` is present, otherwise `false` | Whether to assert marker detection automatically. |

## Endpoint Behaviors

Endpoint behaviors run concurrently with `steps`. They are useful for reusable
endpoint personas such as auto-answering callees or behavior-owned outbound
callers.

### `behaviors`

`behaviors` is a mapping from behavior name to behavior definition.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `initial_state` | Yes | string | Name of the first behavior state. |
| `states` | Yes | non-empty mapping | State definitions keyed by state name. |

### Behavior States

Each state is a mapping.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `entry` | No | list | Actions run whenever the behavior enters this state, including re-entry into the same state. |
| `on` | No | mapping | Event rules active in this state. |

At least one of `entry` or `on` must be non-empty.

Example:

```yaml
states:
  unregistered:
    entry:
      - action: register
    on:
      registered:
        next_state: idle
```

### Behavior Events

Each `on` entry maps an event name to a rule. A rule can include event match
fields, `actions`, `next_state`, and for incoming calls, `save_call_as`.

| Event | Fields | Description |
| --- | --- | --- |
| `registered` | None | Emitted after a behavior-owned `register` entry action succeeds. |
| `scenario_trigger` | `name` required | Emitted by a scripted `trigger_behavior` step. |
| `call_received` | `save_call_as` required, `from_uri` optional, `reuse_alias` optional | Emitted when the endpoint receives an incoming call. |
| `call_state` | `state` required, `call` optional | Emitted when a behavior-owned call changes state. |
| `media_active` | `call` optional | Emitted once when audio media becomes active on a behavior-owned call. |
| `timer_expired` | `timer` required | Emitted when a behavior timer expires. |
| `counter_reached` | `counter` required, `value` required | Emitted synchronously when a behavior counter update reaches an exact integer value in the current state. |

`next_state` is optional. If it is omitted, the behavior remains in the current
state. If `next_state` equals the current state, entry actions run again.

### Behavior Actions

Behavior event rules can reuse most step actions:

- `call`
- `answer`
- `reject`
- `wait_state`
- `wait_media`
- `play_tts`
- `play_wav`
- `record`
- `assert_marker`
- `hold`
- `resume`
- `consult_call`
- `attended_transfer`
- `hangup`
- `record_during_playback`

Behavior-only actions:

| Action | Fields | Description |
| --- | --- | --- |
| `start_timer` | `name` required, `seconds` required | Starts or restarts a behavior-scoped timer. |
| `cancel_timer` | `name` required | Cancels a behavior-scoped timer if active. |
| `set_counter` | `name` required, `value` required | Creates or resets an endpoint-scoped integer counter. |
| `increment_counter` | `name` required, `by` optional | Adds a positive integer amount to an existing counter. Defaults `by` to `1`. |
| `decrement_counter` | `name` required, `by` optional | Subtracts a positive integer amount from an existing counter. Defaults `by` to `1`. |

Entry-only action:

| Action | Fields | Description |
| --- | --- | --- |
| `register` | Same fields as the `register` step, but `client`/`clients` may be omitted | Registers the endpoint client by default and emits `registered` after success. |

Actions not supported inside behaviors:

- `parallel`
- `expect_incoming`
- `video_smoke`
- `wait_behavior_state`
- `trigger_behavior`

### Behavior Action Defaults And Scoping

Inside endpoint behaviors:

- `register` defaults to the endpoint's assigned `client` when neither `client`
  nor `clients` is present.
- `call` and `consult_call` default to the endpoint's assigned `client` when
  `client` is omitted.
- Behavior-created call aliases are scoped by endpoint. A behavior action can
  refer to `outbound`; explicit steps refer to the same call as
  `alice.outbound`.
- A behavior-created call alias cannot overwrite an existing call alias in the
  same endpoint namespace.
- Looping behaviors can set `reuse_alias: true` on `call` or `consult_call`
  actions to replace an existing disconnected `save_as` alias. They can also
  set `reuse_alias: true` on `call_received` rules to replace an existing
  disconnected `save_call_as` alias.
- Alias reuse still fails when the previous call under that alias is active.
  Hang up the previous leg and transition on `call_state: disconnected` before
  entering the state that places or saves the next call.
- Behavior-created recording aliases are also scoped by endpoint.
- Behavior counters are scoped to one endpoint runtime. Counter updates check
  for a matching `counter_reached` rule in the current state before applying
  the triggering rule's `next_state`, so counters can terminate loops without
  placing one extra call.

## Endpoints

`endpoints` assigns configured lab clients to behavior definitions.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `client` | Yes | string | Configured lab client name. |
| `behavior` | Yes | string | Behavior definition name. |

Example:

```yaml
endpoints:
  alice:
    client: alice
    behavior: outbound_caller
```

## Counter-Terminated Loop Example

```yaml
idle:
  entry:
    - action: set_counter
      name: calls_remaining
      value: 3
  on:
    scenario_trigger:
      name: start_call
      next_state: loop

loop:
  entry:
    - action: call
      target: bob
      save_as: outbound
      reuse_alias: true
  on:
    call_state:
      call: outbound
      state: connected
      next_state: connected

connected:
  on:
    call_state:
      call: outbound
      state: disconnected
      actions:
        - action: decrement_counter
          name: calls_remaining
      next_state: loop

    counter_reached:
      counter: calls_remaining
      value: 0
      next_state: done

done:
  entry:
    - action: cancel_timer
      name: hangup_delay
```

## Complete Readiness-Barrier Outbound Example

```yaml
name: readiness-barrier-outbound-call

behaviors:
  outbound_caller:
    initial_state: unregistered
    states:
      unregistered:
        entry:
          - action: register
        on:
          registered:
            next_state: idle

      idle:
        on:
          scenario_trigger:
            name: start_call
            actions:
              - action: call
                target: bob
                save_as: outbound
            next_state: calling

      calling:
        on:
          call_state:
            call: outbound
            state: connected
            next_state: connected

      connected:
        on:
          call_state:
            call: outbound
            state: disconnected
            next_state: idle

  auto_answer:
    initial_state: unregistered
    states:
      unregistered:
        entry:
          - action: register
        on:
          registered:
            next_state: idle

      idle:
        on:
          call_received:
            save_call_as: inbound
            next_state: ringing

      ringing:
        entry:
          - action: answer
            call: inbound
        on:
          call_state:
            call: inbound
            state: connected
            next_state: connected

      connected:
        on:
          call_state:
            call: inbound
            state: disconnected
            next_state: idle

endpoints:
  alice:
    client: alice
    behavior: outbound_caller
  bob:
    client: bob
    behavior: auto_answer

steps:
  - action: wait_behavior_state
    endpoint: alice
    state: idle
    timeout: 30

  - action: wait_behavior_state
    endpoint: bob
    state: idle
    timeout: 30

  - action: trigger_behavior
    endpoint: alice
    name: start_call

  - action: wait_behavior_state
    endpoint: alice
    state: connected
    timeout: 30
```
