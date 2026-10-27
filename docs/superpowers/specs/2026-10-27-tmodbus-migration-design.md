# Migrate to tmodbus, clean up, add tests, move to uv — Design

Date: 2026-10-27
Status: Draft for review

## Intent

Nothing is broken today. The goal is a **cleaner, smaller codebase**. The
centrepiece is replacing pymodbus with [tmodbus](https://github.com/wlcrs/tmodbus)
(1.1.0, BSD-3, Python ≥3.12, deps: `tenacity`, plus `serialx` via the
`async-serial` extra). tmodbus's built-in features replace code we maintain by
hand today. While we're doing that, the rest of `modbus2mqtt.py` is fair game:
the project gains a test suite and moves to uv. It still ships as the same
arm64 Docker image.

Success means the code is noticeably shorter and easier to follow, tests run in
CI, and a user running the container can't tell anything changed: same config,
same MQTT topics and payloads, same Home Assistant entities.

## Goals

1. **Replace pymodbus with tmodbus.** Remove pymodbus completely. Also remove
   pyserial if nothing else needs it (it is imported at
   `modbus2mqtt.py:33`, but appears unused).
2. **Delete hand-written plumbing that tmodbus provides:**
   - The reconnect loop (`modbus2mqtt.py:806-814`, close + connect) →
     `auto_reconnect` on the client.
   - The write retry loop (3 tries, 0.25 s × 1.5 backoff,
     `modbus2mqtt.py:454-492`) → `response_retry_strategy` /
     `retry_on_device_busy` (tenacity).
   - The ad-hoc `asyncio.sleep(0.002)` / `sleep(0.005)` before requests →
     `wait_between_requests`.
   - `result.isError()` checks and parsing error codes out of
     `str(result).split(',')` → tmodbus exceptions (`ModbusResponseError`
     for a device exception response, `ModbusConnectionError` for a lost
     link, `InvalidResponseError` for a bad frame).
3. **Collapse the four near-identical read branches in `Poller.poll()`** into
   a single path keyed by function code.
4. **Fix async correctness.** The main loop calls blocking `time.sleep()`
   inside `async_main()` (`time.sleep(loopBreak)`, `time.sleep(0.010)`).
   Replace these with `await asyncio.sleep()`, and make sure nothing else
   blocks the event loop, so tmodbus reconnect and retry can work.
5. **Shrink `dataTypes.py`.** Replace the hand-written LE/BE pairs for
   int32/uint32/float32 (and anything else that maps cleanly) with a
   table-driven approach using `struct` formats and word order. tmodbus's
   order-aware struct helpers are an option where they fit. Pollers still read
   a block once and slice it per reference. That stays, so per-reference
   `client.read_int32()`-style calls are **not** used for polling.
6. **General cleanup of `modbus2mqtt.py`:**
   - Replace module-level globals (`args`, `verbosity`, `pollers`,
     `deviceList`, `mqc`, `globaltopic`, …) with explicit state passed around
     (e.g. a small app object).
   - Replace `print` + `if verbosity >= N` with `logging`, mapping
     `--verbosity` / `LOG_LEVEL` onto log levels.
   - Split the ~830-line file into a few focused modules: config/CSV
     loading, polling, writing, MQTT/connection, and data types.
     `addToHomeAssistant.py` stays as its own module.
7. **Add a test suite** (pytest + pytest-asyncio or anyio), run in CI.
8. **Move to uv.** Use `pyproject.toml` + `uv.lock`, and delete `setup.py`,
   `requirements.txt` and `MANIFEST.in`. Use a proper package layout so modules
   import as a package (today the Docker image runs `modbus2mqtt.py` from
   `/app` with flat imports like `from dataTypes import DataTypes`).
9. **Keep Docker as the deployment target.** Build with uv in the Dockerfile
   (multi-stage, non-root `worker` user in `dialout`, `/config` and `/logs`
   volumes). The final image contains no build tooling.

## Non-goals

- No new features, new data types, new transports or new CLI options.
- No change to the deployment model (Docker on Raspberry Pi, arm64 only).
- Not publishing to PyPI.
- No changes to `create-openhab-conf.py` beyond keeping it working (it is a
  standalone helper script that doesn't import the package).
- No RS485 echo/strip-TX workaround. The earlier issue was specific to
  hardware that is no longer used.

## Anti-goals (would be failure even if it "works")

- **Any externally visible behaviour change.** Each of these must stay
  identical:
  - CLI flags and their defaults
  - environment variables (`MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`,
    `MQTT_USERNAME`, `MQTT_PASSWORD`, `RTU`, `RTU_BAUD`, `CONFIG_FILE`,
    `LOG_LEVEL`, and the others in use)
  - the CSV config format
  - MQTT topic structure, payload formatting (e.g. float string formatting),
    retain/QoS flags
  - LWT / `connected` online/offline semantics, global and per-device
  - diagnostics topics
  - `reset-autoremove` behaviour
  - Home Assistant discovery payloads and unique IDs (existing HA entities
    must not be orphaned or duplicated)

  Log message wording may change.
- **A bigger codebase.** If a step adds net complexity, it's the wrong step.
- **Regressing polling throughput or timing on RTU.** This includes
  poll rates, the `--set-loop-break` default of 10 ms, and the poller
  disable/re-enable after failures.

## Constraints

- **Transport in production: Modbus RTU over a serial adapter.** TCP
  support must keep working (the `--tcp` flag exists), but RTU is the one that
  matters. RTU in tmodbus uses `serialx` rather than pyserial, which is the
  main technical risk.
- Several Modbus devices (unit IDs) share one bus. tmodbus clients are bound to
  one `unit_id`, and `client.for_unit_id(n)` returns a client on the shared
  transport. Requests to different units must stay serialized on the one
  serial line.
- Python 3.14 (current image `python:3.14-slim`). tmodbus requires ≥3.12.
- The image is arm64-only, built natively on `ubuntu-24.04-arm`. All runtime
  dependencies must install without a compiler on arm64 (verify `serialx` and
  `tenacity`).
- CI publishing rules stay as they are: semver tags, `:latest` frozen
  (`latest=false`), sha tags, branch tags.
- **Release on the v2 tag line** (owner). `:latest` stays frozen at the last v1
  build. See "Tagging" below for the exact sequence.
- The owner will test on the real RTU device before merge. Automated tests
  are the safety net; the hardware test is the final gate.

## Decisions made

- tmodbus replaces pymodbus (owner's decision, for a cleaner, smaller codebase).
- The cleanup covers all of `modbus2mqtt.py`, not just the Modbus layer
  (owner).
- Fix the blocking `time.sleep` in async code (owner).
- Shrink `dataTypes.py` (owner).
- Build a test suite (owner).
- Migrate to uv while still deploying as a Docker container (owner).
- External behaviour stays identical; log wording may change (my call,
  stated during the conversation and not objected to).
- Tests use tmodbus's own async server as an in-process fake device over TCP,
  rather than mocking the client (my call).
- Replace `print` with `logging` (my call).

### Settled after design review (my calls, flagged for the owner to check)

- **Connection-loss behaviour stays as it is today.** Today the main loop
  checks `client.connected`. While the Modbus link is down it doesn't poll or
  write. MQTT `set` messages wait in the queue, and the outage costs at most
  one counted failure. With `auto_reconnect` alone, every poll would raise
  `ModbusConnectionError`. Devices would flip `offline`, autoremove could
  disable every poller, and queued writes would be tried and then lost.
  Rebuild the gate using tmodbus's connection-lost / `on_reconnected`
  callbacks (or an equivalent connected flag), so connection errors pause
  polling instead of counting as device failures. This matters beyond HA:
  rvc2mqtt's virtual inverter treats `<base>/connected` as liveness.
- **Retries match legacy behaviour.** tmodbus's `response_retry_strategy` and
  `retry_on_device_busy` (default on) apply to every request on a client, so
  both are disabled on the client and retries live in `ModbusLink`. pymodbus
  3.11 clients defaulted to `retries=3`, so legacy reads were re-sent on a
  response timeout (exception responses were not retried). `ModbusLink.read`
  therefore re-sends up to 3 times (4 attempts total, no wait) but only after
  a response `TimeoutError`; device exception responses, invalid responses and
  `LinkDown` are not retried. Writes are wrapped in a tenacity strategy
  roughly matching the legacy one (3 attempts, 0.25 s × 1.5 backoff,
  `DeviceError` only).
- **The production start command keeps working.** coach2mqtt's
  `docker-compose.yml` overrides the command with
  `python3 -u modbus2mqtt.py --add-to-homeassistant` and relies on the image's
  `WORKDIR /app`. The new image must still accept exactly that command, for
  example with a thin `/app/modbus2mqtt.py` shim that calls the package's
  `main()`, and with the uv venv's `python3` first on `PATH`. The default CMD
  can be modernised, but the override must not break.
- **The production serial port is a plain device path.** coach2mqtt sets
  `RTU=/dev/ttyUSB0`, passes `/dev` through, and uses no pyserial URL forms or
  RS485 mode. Map the existing flags directly onto
  `create_async_rtu_client` / serialx options. Dropping pyserial URL support
  is acceptable. Update the help text for `--rtu`.
- **Tagging.** `v2.0.0-beta.1` already exists and is what coach2mqtt runs.
  This work continues the v2 line: cut `v2.0.0-beta.2` for owner hardware
  testing, then `v2.0.0` once it's verified.

## Testing approach

- **Characterization tests first, against the current pymodbus code**, then
  keep them green through the migration. They lock in observable behaviour:
  MQTT messages published for a given set of register values, write handling
  from MQTT `set` topics, and HA discovery payloads.
- **Fake device:** a `tmodbus` `AsyncTcpServer` with a `ModbusRequestRouter`
  backed by in-memory coils and registers, run in-process per test. The bridge
  connects with `--tcp`. Tests can make the device return exceptions, stop
  responding, or drop the connection.
- **MQTT:** capture publishes without a real broker (e.g. inject a fake
  paho client or a thin publish interface). A real broker in CI is optional.
- **Coverage to aim for:**
  - Every data type: parse (MQTT → registers) and combine (registers → MQTT),
    including LE/BE, negatives, hibyte/lobyte, packedtime with timezone
    override, list-uint16 and strings.
  - Poll → publish, including `--always-publish` and change-only publishing.
  - Writes: FC5, FC6, FC16 via `--avoid-fc6`, and multi-register writes.
  - Read-only references reject writes.
  - Device exception response → failure counting → offline → poller
    disable/re-enable, and autoremove.
  - Connection loss → polling and writes pause (writes stay queued) →
    reconnect → polling resumes, with `connected` behaving exactly as it
    does today (see "Connection-loss behaviour" below).
  - Config/CSV parsing, including bad rows.
- The RTU path can't be covered end to end in CI. Owner hardware testing covers
  it.
- CI runs `uv run pytest` (and lint, if added) on PRs. Image build/push stays
  as is.

## Left to the builder

- Exact module split and names.
- Shape of the app/state object.
- Whether to use tmodbus's `OrderAwareStruct` or plain `struct` inside the
  table-driven `dataTypes`.
- The exact tenacity retry strategy, as long as write retry behaviour stays
  roughly equivalent (a few attempts with short backoff).
- How to map `--set-modbus-timeout`, parity, baud, stopbits and bytesize
  onto `create_async_rtu_client` / serialx options.
- Linting/formatting tooling (e.g. ruff), if any.
- Order of work, as long as characterization tests exist before behaviour-
  bearing code is changed.
