# tmodbus Migration, Cleanup, Tests and uv — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pymodbus with tmodbus, restructure the bridge into small modules with no globals, shrink the data-type code, add a pytest suite, and package with uv. The Docker image must behave identically from the outside.

**Architecture:** First, lock in today's observable behaviour with end-to-end characterization tests. These run the real bridge in-process against a fake Modbus device (a tmodbus `AsyncTcpServer`) and a fake paho client. Then rebuild behind those tests:
- `datatypes.py`: table-driven data types.
- `config.py` + `model.py`: CSV loading and the Device/Poller/Reference state.
- `modbus.py`: a `ModbusLink` on tmodbus that sorts failures into device errors and link-down.
- `mqtt.py`, `bridge.py`, `cli.py`: the MQTT side, the main loop, and the entry point.

**Tech Stack:** Python 3.14 (floor 3.12), uv, tmodbus[async-serial] ≥1.1, paho-mqtt 2.1.0, tenacity (via tmodbus), pytest + pytest-asyncio, Docker (arm64).

**Spec:** `docs/superpowers/specs/2026-10-27-tmodbus-migration-design.md`

## Global Constraints

- External behaviour must stay identical. That covers:
  - CLI flags and defaults
  - env vars: `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `RTU`, `RTU_BAUD`, `CONFIG_FILE`, `LOG_LEVEL`
  - CSV format
  - MQTT topics, payload formatting, retain/QoS
  - global and per-device LWT / `connected` semantics
  - diagnostics topics
  - `reset-autoremove`
  - HA discovery payloads and unique IDs

  Log wording may change.
- coach2mqtt runs `python3 -u modbus2mqtt.py --add-to-homeassistant` with `WORKDIR /app`, `RTU=/dev/ttyUSB0`, `LOG_LEVEL=1`. That command must keep working.
- Link loss pauses polling and writes. It is never counted as a device failure, and writes stay queued. Only device errors and timeouts count toward `failcounter`.
- Writes are retried on `DeviceError`, with 3 attempts and backoff starting at 0.25 s ×1.5. Reads are re-sent (up to 3 re-sends, 4 attempts, no wait) only after a response timeout, matching legacy pymodbus `retries=3`; device exception responses, invalid responses and `LinkDown` are never retried. `retry_on_device_busy=False`.
- No blocking calls inside the event loop (no `time.sleep`).
- arm64-only image, built natively. `:latest` stays frozen (`latest=false`). Release on the v2 line: `v2.0.0-beta.2` for hardware testing, then `v2.0.0`.
- The final `modbus2mqtt/` package must have fewer total lines than today's 1,329 (`wc -l modbus2mqtt/*.py`).
- Run everything with uv: `uv run pytest`.

## Review Focus

1. **Serial adapter unplugged mid-run.** The bridge keeps running, devices aren't flipped `offline` by link errors, and it resumes when the port returns. Test in Task 6: `test_link_down_is_not_device_error`.
2. **`docker stop` while a request is blocked in reconnect.** The process exits within a few seconds and publishes global `connected=offline`. Test in Task 7: `test_sigterm_during_reconnect_exits_promptly`.
3. **Garbage on a `set` topic** (non-numeric, out of range, empty, invalid UTF-8). Nothing is written, nothing crashes, and polling continues. Test in Task 3: `test_garbage_set_payloads_ignored`.
4. **CSV saved with a UTF-8 BOM or CRLF line endings** (e.g. edited on Windows). It loads normally. Today a BOM silently yields "No pollers". Test in Task 5: `test_load_config_bom_and_crlf`.
5. **`LOG_LEVEL` outside 0–4.** 5+ behaves as 4 (DEBUG) and negative values as 0. Test in Task 7: `test_verbosity_mapping`.

---

### Task 1: uv packaging, importable package, Docker shim, CI test job

Behaviour stays the same and pymodbus remains. This task only makes the code an installable package, runnable through both entry points, with a test runner.

**Files:**
- Create: `pyproject.toml`, `uv.lock` (generated), `modbus2mqtt/cli.py`, `docker/modbus2mqtt.py`, `tests/__init__.py`, `tests/test_cli.py`
- Modify:
  - `modbus2mqtt/modbus2mqtt.py`: argparse moves to `cli.py`, and `async_main` takes `args`
  - `modbus2mqtt/addToHomeAssistant.py:2`: relative import
  - `modbus2mqtt/__init__.py`, `modbus2mqtt/__main__.py`
  - `Dockerfile`
  - `.github/workflows/release_github_on_tag.yml`
- Delete: `setup.py`, `requirements.txt`, `MANIFEST.in`

**Interfaces:**
- Produces:
  - `modbus2mqtt.cli.build_parser() -> argparse.ArgumentParser`: the exact flags, defaults and env lookups from today's `modbus2mqtt.py:544-581`.
  - `modbus2mqtt.cli.run(argv: list[str] | None = None) -> Coroutine[None]`: parses args and awaits the bridge.
  - `modbus2mqtt.cli.main(argv: list[str] | None = None) -> None`: `asyncio.run(run(argv))`.
  - `modbus2mqtt.__version__: str` (from `importlib.metadata.version("modbus2mqtt")`).

- [ ] **Step 1: Create the branch and commit the spec and plan**

```bash
git switch -c tmodbus-migration
git add docs/superpowers
git commit -m "docs: tmodbus migration design and plan"
```

- [ ] **Step 2: Write `pyproject.toml`**
  - `name = "modbus2mqtt"`, `version = "2.0.0b2"`, `requires-python = ">=3.12"`.
  - `dependencies = ["paho-mqtt==2.1.0", "pymodbus==3.11.1", "pyserial"]`. pymodbus and pyserial are removed in Task 7.
  - `[project.scripts] modbus2mqtt = "modbus2mqtt.cli:main"`.
  - Build backend: hatchling, package `modbus2mqtt`.
  - `[dependency-groups] dev = ["pytest", "pytest-asyncio", "tmodbus>=1.1,<2"]`.
  - `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`.
  - Run `uv lock`.

- [ ] **Step 3: Write the failing tests in `tests/test_cli.py`**

```python
def test_parser_env_defaults(monkeypatch):
    for k, v in {"MQTT_HOST": "broker", "MQTT_PORT": "1999", "MQTT_TOPIC": "x/",
                 "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p", "RTU": "/dev/ttyUSB0",
                 "RTU_BAUD": "19200", "CONFIG_FILE": "/c.csv", "LOG_LEVEL": "1"}.items():
        monkeypatch.setenv(k, v)
    a = build_parser().parse_args([])
    assert (a.mqtt_host, a.mqtt_port, a.mqtt_topic, a.mqtt_user, a.mqtt_pass) == ("broker", 1999, "x/", "u", "p")
    assert (a.rtu, a.rtu_baud, a.config, a.verbosity) == ("/dev/ttyUSB0", 19200, "/c.csv", 1)

def test_parser_plain_defaults(monkeypatch):
    for k in ("MQTT_HOST","MQTT_PORT","MQTT_TOPIC","MQTT_USERNAME","MQTT_PASSWORD","RTU","RTU_BAUD","CONFIG_FILE","LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    a = build_parser().parse_args([])
    assert (a.mqtt_host, a.mqtt_port, a.mqtt_topic, a.rtu_baud, a.rtu_parity) == ("localhost", None, "modbus/", 9600, "none")
    assert (a.tcp_port, a.set_modbus_timeout, a.verbosity, a.diagnostics_rate, a.set_loop_break) == (502, 1.0, 3, 0, None)

def test_module_help_runs():
    r = subprocess.run([sys.executable, "-m", "modbus2mqtt", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--rtu" in r.stdout and "--avoid-fc6" in r.stdout

def test_docker_shim_help_runs(tmp_path):
    shim = tmp_path / "modbus2mqtt.py"
    shim.write_text(Path("docker/modbus2mqtt.py").read_text())
    r = subprocess.run([sys.executable, "-u", "modbus2mqtt.py", "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and "--rtu" in r.stdout
```

- [ ] **Step 4: Run them and confirm they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL (`modbus2mqtt.cli` doesn't exist).

- [ ] **Step 5: Implement**
  - **`cli.py`:** move `build_parser()` out of `async_main` unchanged.
  - **`async_main`:** becomes `async_main(args)`.
  - **Imports:** switch to package-relative (`from .dataTypes import DataTypes`, `from .addToHomeAssistant import HassConnector`).
  - **`__main__.py`:** calls `cli.main()`.
  - **`docker/modbus2mqtt.py`:** the shim. It must drop its own directory from `sys.path` before importing. Otherwise `import modbus2mqtt` resolves to the shim itself:
    ```python
    import sys
    sys.path.pop(0)  # /app holds this file; without this, "import modbus2mqtt" imports it
    from modbus2mqtt.cli import main
    main()
    ```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest -v`
Expected: 4 passed.

- [ ] **Step 7: Rewrite the `Dockerfile` with uv**
  - **Builder stage** (`python:3.14-slim`):
    - Copy uv in with `COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv`.
    - Set `UV_PROJECT_ENVIRONMENT=/opt/venv`, `UV_COMPILE_BYTECODE=1` and `UV_LINK_MODE=copy`.
    - Run `uv sync --locked --no-dev --no-editable`.
  - **Final stage** (`python:3.14-slim`):
    - Keep the `worker` user in `dialout`, plus `/config` and `/logs`.
    - `COPY --from=builder /opt/venv /opt/venv`.
    - Copy `docker/modbus2mqtt.py` to `/app/modbus2mqtt.py`.
    - `ENV PATH="/opt/venv/bin:$PATH"`, `WORKDIR /app`, `CMD ["python3","-u","modbus2mqtt.py"]`.

- [ ] **Step 8: Verify the image**

Run: `docker build -t m2m:test . && docker run --rm m2m:test python3 -u modbus2mqtt.py --help`
Expected: help text that includes `--rtu`. Also run `docker run --rm m2m:test sh -c 'which python3'` and expect `/opt/venv/bin/python3`.

- [ ] **Step 9: Add a `test` job to the workflow and gate the build on it**
  - The new job runs on `ubuntu-24.04-arm` using `astral-sh/setup-uv@v6`, then `uv sync --locked` and `uv run pytest`.
  - `build` gets `needs: test`.
  - Leave triggers and tags untouched.
  - Delete `setup.py`, `requirements.txt`, `MANIFEST.in`.
  - Add `.venv` to `.dockerignore` (create the file and include `.git`, `tests`, `docs`).

- [ ] **Step 10: Commit**

```bash
git add -A && git commit -m "build: package with uv, add cli module, docker shim and CI test job"
```

---

### Task 2: Data-type facade and characterization tests

Pin every data type's conversions behind the interface that Task 4 will reimplement.

**Files:**
- Create: `modbus2mqtt/datatypes.py`, `tests/test_datatypes.py`

**Interfaces:**
- Produces, in `modbus2mqtt/datatypes.py`:
  ```python
  @dataclass(frozen=True)
  class DataType:
      name: str        # canonical: "uint16","int16","uint32LE","uint32BE","int32LE","int32BE",
                       # "float32LE","float32BE","bool","string","list-uint16","packedtime","hibyte","lobyte"
      reg_amount: int
      def parse(self, payload: str, ref) -> int | bool | list[int] | None   # MQTT -> register value(s); None = invalid
      def combine(self, regs: list, ref) -> int | float | bool | str | None  # registers (always a list) -> published value
  def parse_data_type(conf: str | None) -> DataType
  ```
  `ref` is only read for `ref.json.get("timezone")` (packedtime). In this task, the implementation delegates to the old `DataTypes` functions through a stub object carrying `stringLength`, `regAmount` and `json`.

- [ ] **Step 1: Write parametrized tests in `tests/test_datatypes.py`**, using `Ref = SimpleNamespace(json={})`. Cover:
  - **`reg_amount`:** `None`/`""`/`"uint16"`→1; `"string10"`→5; `"string9"`→4 (odd rounds down); `"string200"`→50 (capped at 100 chars); `"list-uint16-3"`→3; `"list-uint16-99"`→50; `"packedtime"`→3; each 32-bit type→2.
  - **Name:** `parse_data_type("string10").name == "string"`, `parse_data_type("list-uint16-3").name == "list-uint16"`.
  - **Parse → registers:**
    - `uint16("65535")==65535`, `uint16("65536") is None`, `uint16("x") is None`.
    - `int16("-1")==0xFFFF`, `int16("32768") is None`.
    - `uint32LE("65537")==[1,1]`, `uint32BE("65538")==[2,1]`.
    - `int32LE("-2")==[0xFFFF,0xFFFE]`, `int32BE("-2")==[0xFFFE,0xFFFF]`.
    - `float32LE("1.5")==[0x3FC0,0]`, `float32BE("1.5")==[0,0x3FC0]`.
    - `bool("true") is True`, `bool("0") is False`, `bool("yes") is None`.
    - `string10("AB")==[0x4142]`, `string4("ABCDE") is None`.
    - `list-uint16-3("1 2 3")==[1,2,3]`, `list-uint16-3("1 2") is None`.
    - `hibyte("2")==0x0200`, `lobyte("256") is None`.
    - `packedtime("2026-10-27 13:45:09")==[(26<<8)|10, (27<<8)|13, (45<<8)|9]`.
  - **Combine → value:**
    - `int16([0xFFFF])==-1`, `uint16([7])==7`.
    - `uint32LE([1,1])==65537`, `uint32BE([2,1])==65538`.
    - `int32LE([0xFFFF,0xFFFE])==-2`, `int32BE([0xFFFE,0xFFFF])==-2`.
    - `str(float32BE([0x6666,0x3F8C]))=="1.100000023841858"`, `str(float32LE([0x3F8C,0x6666]))=="1.100000023841858"`.
    - `bool([1]) is True`.
    - `string([0x4142,0x4344])=="ABCD"`, `list-uint16([1,2])=="1 2 "` (trailing space, as today).
    - `hibyte([0x1234])==0x12`, `lobyte([0x1234])==0x34`.
    - `packedtime([(26<<8)|10,(27<<8)|13,(45<<8)|9], Ref(json={"timezone":"America/Los_Angeles"}))=="2026-10-27T13:45:09"`.
    - `packedtime` with `json={}` returns that local time with an offset suffix (assert `startswith("2026-10-27T13:45:09")` and `len > 19`).
    - `packedtime([0,0,0]) is None`.
  - **Packedtime aware input:** `packedtime.parse("2026-10-27T20:45:09Z", Ref(json={"timezone":"America/Los_Angeles"}))` gives hour 13.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_datatypes.py -v`
Expected: FAIL (no `modbus2mqtt.datatypes`).

- [ ] **Step 3: Implement the facade**
  - `parse_data_type` calls the old `DataTypes.parseDataType` on a stub to get `regAmount`/`parse`/`combine`, then wraps them.
  - `combine` always receives a list.
  - Name canonicalization strips the length suffix from `string*` and `list-uint16-*`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_datatypes.py -v`
Expected: all pass. If a value differs from today's real behaviour, fix the **test** to match the old code and note it in the commit message. These tests characterize the old behaviour; they don't specify new behaviour.

- [ ] **Step 5: Commit** — `test: characterize data type conversions behind DataType facade`

---

### Task 3: End-to-end harness and characterization tests (against current pymodbus code)

**Files:**
- Create: `tests/fakes/__init__.py`, `tests/fakes/modbus_device.py`, `tests/fakes/mqtt.py`, `tests/harness.py`, `tests/test_bridge_e2e.py`, `tests/golden/ha_discovery.json`

**Interfaces:**
- Produces, in `tests/fakes/modbus_device.py`:
  ```python
  class FakeModbusDevice:
      def __init__(self, unit_ids: Iterable[int] = (1,)) -> None
      coils: dict[int, dict[int, bool]]; discrete: dict[int, dict[int, bool]]   # unit -> addr -> value, default False
      holding: dict[int, dict[int, int]]; inputs: dict[int, dict[int, int]]   # default 0
      writes: list[tuple[int, int, int, object]]   # (unit, function_code, address, value(s)) in arrival order
      fail_with: type[Exception] | None            # e.g. IllegalDataAddressError -> every request answered with that exception
      port: int                                    # free port chosen once, reused across restarts
      async def start(self) -> None
      async def stop(self) -> None                 # closes the listener AND drops open client connections
  ```
  Built on tmodbus `AsyncTcpServer` + `ModbusRequestRouter`. It handles FC1/2/3/4 reads and FC5/6/15/16 writes per unit.
- Produces, in `tests/fakes/mqtt.py`:
  ```python
  @dataclass
  class Published: client_id: str; topic: str; payload: bytes; qos: int; retain: bool
  class FakeBroker:
      clients: list["FakeMqttClient"]; published: list[Published]
      def client_factory(self, *args, **kwargs) -> "FakeMqttClient"   # signature-compatible with paho.mqtt.client.Client
      def inject(self, topic: str, payload: str | bytes) -> None   # paho MQTTMessage to on_message of each client subscribed to a matching filter (paho topic_matches_sub)
      def payloads(self, topic: str) -> list[bytes]
      def last(self, topic: str) -> Published | None
  class FakeMqttClient:
      # will_set, username_pw_set, tls_set, tls_insecure_set, connect, loop_start, loop_stop,
      # disconnect, subscribe, publish(topic, payload=None, qos=0, retain=False) -> obj with .rc == 0, is_connected
      will: tuple[str, bytes, int, bool] | None
  ```
  - `loop_start()` calls `on_connect(self, None, flags, ReasonCode(PacketTypes.CONNACK, "Success"), None)` synchronously.
  - `publish` converts payloads exactly as paho 2.1 does: `str`→utf-8, `bytes` kept, `int`/`float`→`str(x).encode()`, `None`→`b""`.
- Produces, in `tests/harness.py`:
  ```python
  @asynccontextmanager
  async def running_bridge(tmp_path, monkeypatch, csv_text: str, device: FakeModbusDevice, *extra_args: str) -> AsyncIterator[FakeBroker]
  async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None   # polls every 10 ms with asyncio.sleep; raises AssertionError on timeout
  ```
  `running_bridge` does the following:
  - Writes the CSV and monkeypatches `paho.mqtt.client.Client` with `broker.client_factory`.
  - Saves and restores the SIGINT/SIGTERM handlers.
  - Calls `_reset_legacy_globals()`, which clears `pollers`, `deviceList` and `referenceList`, replaces `writeQueue`, and resets `adder`. **Task 7 deletes this.**
  - Starts `cli.run([...])` as a task with `--tcp 127.0.0.1 --tcp-port <port> --config <csv> --mqtt-host fake --verbosity 4 *extra_args`.
  - Yields the broker.
  - On exit, calls `signal.raise_signal(SIGTERM)`, awaits the task (5 s timeout), and ignores `SystemExit(0)`.

  Shared CSV fixture `BASIC_CSV`, with unit 1 and a poller rate of `0.05`:
  - Holding poller `dev` (refs 0–9) with refs `u16` uint16 r, `i16` int16 rw, `f32` float32BE r, `s32` int32BE rw, `name` string4 r, `hi` hibyte r, `clock` packedtime r.
  - Coil poller `dev` (0–3) with `relay` rw.
  - Input-register poller `dev` (0–1) with `inp` r.

- [ ] **Step 1: Write the fakes and harness, plus `test_harness_smoke`**
  - Start the device and run the bridge.
  - `wait_for` that `dev/state/u16` is published.
  - Assert `broker.last("modbus/connected").payload == b"online"`.

- [ ] **Step 2: Run the smoke test and confirm it passes against today's code**

Run: `uv run pytest tests/test_bridge_e2e.py::test_harness_smoke -v`
Expected: PASS.

- [ ] **Step 3: Write the characterization tests in `tests/test_bridge_e2e.py`**, one function each:
  - **`test_poll_publishes_retained_state`:** holding 0=7, 1=0xFFFF, 2..3=float 1.5 BE, 4..5=-2 int32BE, 6..7="ABCD", 8=0x1234; coil 0 True; input 0=42. Expect these payloads on `modbus/dev/state/<ref>`, all `retain=True`, QoS 0:
    - `u16`=`b"7"`, `i16`=`b"-1"`, `f32`=`b"1.5"`, `s32`=`b"-2"`
    - `name`=`b"ABCD"`, `hi`=`b"18"`, `relay`=`b"True"`, `inp`=`b"42"`
  - **`test_publish_only_on_change`:** after the first publish of `u16`, wait 0.3 s and expect one publish. Change the register and expect a second.
  - **`test_always_publish`:** same as above but with `--always-publish`; ≥3 publishes in 0.3 s.
  - **`test_write_holding_fc6`:** inject `modbus/dev/set/i16` `"-5"`. Expect `device.writes[-1] == (1, 6, 1, 0xFFFB)`, then state `i16`=`b"-5"`.
  - **`test_write_avoid_fc6_uses_fc16`:** with `--avoid-fc6`, expect FC 16 at address 1 with `[0xFFFB]`.
  - **`test_write_int32_uses_fc16`:** inject `s32` `"-2"`. Expect `(1, 16, 4, [0xFFFE, 0xFFFF])`.
  - **`test_write_coil_fc5`:** inject `relay` `"1"`. Expect `(1, 5, 0, True)`.
  - **`test_read_only_ref_not_writable`:** inject `u16` `"3"`. No write in 0.3 s.
  - **`test_garbage_set_payloads_ignored`** (Review Focus 3): inject `i16` with `"abc"`, `"99999"`, `""`, `b"\xff\xfe"`. No writes, and `u16` still updates after its register changes.
  - **`test_device_exception_goes_offline_and_recovers`:** after online, set `fail_with=IllegalDataAddressError`. `modbus/dev/connected` becomes `b"offline"` (retain, QoS 1) only after ≥2 consecutive failures. Clear the failure and it goes back to `b"online"`.
  - **`test_autoremove_and_reset`:** with `--autoremove`, fail the device and wait until no reads arrive for 0.3 s. Clear the failure, then inject `modbus/reset-autoremove` `"1"`. Polling resumes.
  - **`test_link_loss_pauses_and_queues_writes`:**
    - After online, `await device.stop()`, then inject `i16` `"9"`.
    - Wait 0.5 s. Assert no `offline` on `modbus/dev/connected`.
    - `await device.start()`. Expect the write `(1, 6, 1, 9)` and polling to resume.
  - **`test_per_device_lwt_and_global_lwt`:**
    - The global client's `will == ("modbus/connected", b"offline", 2, True)`.
    - The device client's `will == ("modbus/dev/connected", b"offline", 1, True)`.
    - On shutdown, `modbus/connected` and `modbus/dev/connected` get `b"offline"`.
  - **`test_diagnostics`:** with `--diagnostics-rate 1`, expect `modbus/dev/state/diagnostics_errors_percent` and `..._total` (retain, QoS 1).
  - **`test_ha_discovery_matches_golden`:**
    - Run with `--add-to-homeassistant`, plus a `clock` ref with HA json `{"timezone":"America/Los_Angeles"}`.
    - Parse the payload on `homeassistant/device/modbus_dev_1/config` and compare it to `tests/golden/ha_discovery.json`, ignoring `o.sw`.
    - Inject `homeassistant/status` `"online"` and expect discovery to be republished.
    - Generate the golden file once from today's code (dump it from the test with an env flag `UPDATE_GOLDEN=1`), read it to check it's sensible, and commit it.
  - **`test_timezone_set_via_mqtt`:**
    - Inject `modbus/dev/set/clock/timezone` `"Europe/Berlin"`. Expect state `modbus/dev/state/clock/timezone`=`b"Europe/Berlin"` (QoS 1, retain), and `clock` is republished.
    - Inject `"Not/AZone"`. The tz state snaps back to `b"Europe/Berlin"`.

- [ ] **Step 4: Run them all against today's code**

Run: `uv run pytest -v`
Expected: all pass. Where today's behaviour differs from an expectation above, make the test assert **what the old code really does**, and list each such difference in the commit message so the owner can see it. Exception: if link loss produces `offline`, stop and report, because the spec relies on it not doing so.

- [ ] **Step 5: Commit** — `test: end-to-end characterization harness against current bridge`

---

### Task 4: Table-driven data types

**Files:**
- Modify:
  - `modbus2mqtt/datatypes.py`: real implementation
  - `modbus2mqtt/modbus2mqtt.py`: `Reference` uses `ref.dtype`, and write-publish passes a list
  - `modbus2mqtt/addToHomeAssistant.py`: uses `ref.dtype.name`
  - `tests/test_datatypes.py`: add the float test below
- Delete: `modbus2mqtt/dataTypes.py`

**Interfaces:**
- Consumes: `DataType` / `parse_data_type` from Task 2. Their signatures stay unchanged.
- Produces: `Reference.dtype: DataType`, replacing `ref.parse` / `ref.combine` / `ref.regAmount` / `ref.stringLength`. HA checks use `ref.dtype.name == "bool"` / `== "packedtime"`.

- [ ] **Step 1: Add failing tests**
  - `test_float_combine_returns_float`: `isinstance(parse_data_type("float32BE").combine([0,0x3FC0], Ref) , float)`.
  - In `test_bridge_e2e.py`, `test_scaled_float_publishes`: `f32` with scaling `2` and register value 1.5 publishes `b"3.0"`. Today this raises `TypeError` (str × float) and never publishes; this is a deliberate fix.

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest -k "float_combine_returns_float or scaled_float" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**
  - Build one registry `dict[str, DataType]` for the fixed-size types.
  - Encode all 32-bit types with a single helper around `struct` (`">I"`, `">i"`, `">f"` → two 16-bit words). "LE" in this codebase means **high word first**; "BE" swaps the words. Keep those names and that meaning.
  - `string*` and `list-uint16-*` build a `DataType` with the parsed length. Keep the same caps and log messages.
  - Float `combine` returns `float`.
  - `Reference.check_publish` and the write path always hand `combine` a list (wrap a scalar write value in `[value]`).
  - Remove all `try: len(val)` scalar handling.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v`
Expected: all pass, including every Task 2 and Task 3 test unchanged.

- [ ] **Step 5: Commit** — `refactor: table-driven data types; fix scaling of float32 values`

---

### Task 5: Config loading and model objects without globals

**Files:**
- Create: `modbus2mqtt/model.py`, `modbus2mqtt/config.py`, `tests/test_config.py`
- Modify: `modbus2mqtt/modbus2mqtt.py` (use them; delete the inline CSV parsing and the old `Device`/`Poller`/`Reference` classes)

**Interfaces:**
- Produces, in `modbus2mqtt/model.py`. These are plain state with no MQTT or Modbus calls:
  ```python
  class Device:     name: str; unit_id: int; writable: dict[str, Reference]; occupied_topics: set[str]
                    error_count: int; poll_count: int; connected: bool | None
  class Reference:  topic: str; address: int; dtype: DataType; rw: str; scale: float | None
                    json: dict; publish: bool; device: Device | None; relative: int | None
                    write_fc: int | None; lastval: object
  class Poller:     topic: str; rate: float; unit_id: int; fc: int; address: int; size: int
                    device: Device; references: list[Reference]; disabled: bool; failcounter: int
                    connected: bool; next_due: float
  ```
- Produces, in `modbus2mqtt/config.py`:
  ```python
  @dataclass
  class Config: pollers: list[Poller]; devices: list[Device]; references: list[Reference]   # references = HA/publishable list, same membership rules as today's referenceList
  def load_config(path: str | os.PathLike) -> Config
  ```
  `load_config` keeps every rule from `modbus2mqtt.py:604-660` and `Poller.addReference` (`:261-293`):
  - poller/poll and reference/ref row types
  - column mapping
  - holding/input ≤123 and coil/input_status ≤2000, otherwise the poller is skipped and its refs orphaned
  - coil/input_status refs forced to bool
  - duplicate topic per device ignored
  - out-of-range refs not polled
  - write FC 6 for holding and 5 for coil; discrete/input refs not writable
  - scaling parse
  - HA json with a `publish` key popped

  It opens the file with `encoding="utf-8-sig", newline=""`.

- [ ] **Step 1: Write the tests in `tests/test_config.py`**
  - **`test_load_basic`:** the `BASIC_CSV` from Task 3 (import it from `tests/harness.py`) gives:
    - 3 pollers and 1 device
    - `device.writable.keys() == {"i16","s32","relay"}`
    - `i16.write_fc == 6`, `relay.write_fc == 5`
    - `f32.dtype.name == "float32BE"`, `relay.dtype.name == "bool"`
  - **`test_oversized_poller_skipped`:** a holding poller of size 124 is dropped and its refs are not loaded.
  - **`test_duplicate_topic_ignored`.**
  - **`test_out_of_range_ref_not_polled`:** not in `poller.references`.
  - **`test_input_register_ref_not_writable`:** rw `"rw"` on an input_register poller gives `write_fc is None`.
  - **`test_publish_false_in_json`:** `ref.publish is False`, and `"publish" not in ref.json`.
  - **`test_bad_scaling_ignored`:** `scale is None`.
  - **`test_reference_without_poller_ignored`.**
  - **`test_load_config_bom_and_crlf`** (Review Focus 4): the same CSV saved with a BOM and `\r\n` loads identically.
  - **`test_example_csv_loads`:** `load_config("example.csv")` gives ≥1 poller.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (no module).

- [ ] **Step 3: Implement**
  - Implement `model.py` and `config.py`.
  - Switch `modbus2mqtt.py` to `load_config(args.config)`.
  - Failure accounting, publishing and diagnostics temporarily stay as functions in `modbus2mqtt.py` that take the objects explicitly. Task 7 moves them.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v`
Expected: all pass.

- [ ] **Step 5: Commit** — `refactor: extract CSV config loading and plain model objects`

---

### Task 6: `ModbusLink` on tmodbus

**Files:**
- Create: `modbus2mqtt/modbus.py`, `tests/test_modbus_link.py`
- Modify: `pyproject.toml`: add `tmodbus[async-serial]>=1.1,<2` to runtime deps

**Interfaces:**
- Produces, in `modbus2mqtt/modbus.py`:
  ```python
  class DeviceError(Exception): ...   # device answered with a Modbus exception, timed out, or sent an invalid response -> counts as a poll failure
  class LinkDown(Exception): ...      # transport is unavailable (connect/reconnect failed, port gone) -> pause, never counted

  class ModbusLink:
      def __init__(self, transport_client: tmodbus.AsyncModbusClient) -> None
      connected: bool                                    # False after LinkDown until a request succeeds again
      async def connect(self) -> None                    # raises LinkDown
      async def close(self) -> None
      async def read(self, unit_id: int, fc: int, address: int, count: int) -> list[int] | list[bool]   # fc in {1,2,3,4}
      async def write(self, unit_id: int, fc: int, address: int, value: int | bool | list[int], *, force_multiple: bool = False) -> int
          # fc 5 -> write_single_coil; fc 6 -> write_single_register, or write_multiple_registers when value is a list
          # or force_multiple; returns the fc actually used (5, 6 or 16). Retries DeviceError: 3 attempts,
          # wait_exponential(multiplier=0.25, exp_base=1.5); LinkDown is not retried here.

  def create_link(args: argparse.Namespace) -> ModbusLink
  ```
  `create_link`:
  - **`--rtu`:** `create_async_rtu_client(args.rtu, unit_id=1, timeout=args.set_modbus_timeout, ...)` with baud `args.rtu_baud`, parity N/O/E from `args.rtu_parity`, stopbits 1, bytesize 8. Get the exact option names from `tmodbus.SerialXOptions` in the installed package.
  - **`--tcp`:** `create_async_tcp_client(args.tcp, args.tcp_port, unit_id=1, timeout=args.set_modbus_timeout)`.
  - **Both:** `auto_reconnect=True`, `retry_on_device_busy=False`, `wait_between_requests=0.002`.
  - **Neither:** log "You must specify a modbus access method, either --rtu or --tcp" and `sys.exit(1)`.

  Per-unit clients come from `client.for_unit_id(n)` and are cached in a dict.

  **Exception mapping.** Check the class names against the installed `tmodbus.exceptions`:

  | tmodbus raises | `ModbusLink` raises |
  |---|---|
  | `ModbusResponseError` and its subclasses | `DeviceError` |
  | `InvalidResponseError` | `DeviceError` |
  | `TimeoutError` | `DeviceError` |
  | `ModbusConnectionError`, `OSError` | `LinkDown` |
  | `RequestRetryFailedError` caused by `ModbusConnectionError`/`OSError` | `LinkDown` |
  | `RequestRetryFailedError`, any other cause | `DeviceError` |

  Background (tmodbus 1.1 source): with auto-reconnect, a request made while the link is down blocks inside the client's lock. tmodbus retries the reconnect (exponential backoff 0.1–10 s, ~60 s budget), then raises `RequestRetryFailedError`. Reconnection is lazy and happens on the next request.

- [ ] **Step 1: Write the tests in `tests/test_modbus_link.py`**, using `FakeModbusDevice` (units 1 and 2) and `ModbusLink` over `create_async_tcp_client`:
  - **`test_read_each_fc`:** FC1/2 return `list[bool]` and FC3/4 return `list[int]` with the seeded values.
  - **`test_two_units_share_link`:** reads from unit 1 and unit 2 return their own values.
  - **`test_device_exception_raises_device_error`.**
  - **`test_write_paths`:**
    - FC5 → `(u,5,a,True)`, FC6 scalar → `(u,6,a,v)`.
    - FC6 with a list → FC16 and the method returns `16`.
    - `force_multiple=True` with a scalar → FC16 `[v]`.
  - **`test_write_retries_device_error`:** fail the first 2 requests (add a `fail_next: int` counter to `FakeModbusDevice`); the 3rd succeeds and `write` returns. With `fail_next=3` it raises `DeviceError` after 3 attempts.
  - **`test_reads_not_retried`:** with `fail_next=1`, a read raises `DeviceError` and the device saw exactly 1 request.
  - **`test_link_down_is_not_device_error`** (Review Focus 1):
    - Build the link with a short reconnect strategy (`AsyncRetrying(stop=stop_after_attempt(2), wait=wait_fixed(0.05))`).
    - `await device.stop()`. A read raises `LinkDown` and `link.connected is False`.
    - `await device.start()`. A read succeeds and `link.connected is True`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_modbus_link.py -v`
Expected: FAIL (no module).

- [ ] **Step 3: Implement `modbus.py`**
  - Read dispatch is a dict from fc to client method name.
  - The write retry is a tenacity `AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.25, exp_base=1.5), retry=retry_if_exception_type(DeviceError), reraise=True)`.
  - `create_link` accepts optional `reconnect: AsyncRetrying | None` to support the test above.

- [ ] **Step 4: Run them and confirm they pass**

Run: `uv run pytest tests/test_modbus_link.py -v`
Expected: all pass.

- [ ] **Step 5: Commit** — `feat: ModbusLink on tmodbus with device-error vs link-down classification`

---

### Task 7: Bridge, MQTT and CLI rewrite; remove pymodbus

**Files:**
- Create: `modbus2mqtt/mqtt.py`, `modbus2mqtt/bridge.py`
- Rename: `modbus2mqtt/addToHomeAssistant.py` → `modbus2mqtt/homeassistant.py` (logging instead of print; `o.sw` = `__version__`)
- Modify: `modbus2mqtt/cli.py`, `tests/harness.py` (delete `_reset_legacy_globals`), `pyproject.toml` (drop `pymodbus`, `pyserial`; `uv lock`)
- Delete: `modbus2mqtt/modbus2mqtt.py`
- Test: `tests/test_bridge_e2e.py` (unchanged tests plus two new ones), `tests/test_cli.py`

**Interfaces:**
- Consumes: `Config`/`load_config` (Task 5), `ModbusLink`/`create_link`/`DeviceError`/`LinkDown` (Task 6), `DataType` (Task 4).
- Produces:
  - **`modbus2mqtt/mqtt.py`:**
    ```python
    class MqttSide:
        def __init__(self, args, prefix: str, devices: list[Device]) -> None   # global client + one client per device, LWTs as today
        inbox: queue.SimpleQueue[mqtt.MQTTMessage]                          # filled from paho threads
        initial_connection_made: bool
        on_connected: Callable[[], None] | None                             # bridge hook: subscribe HA status / publish discovery
        def start(self) -> None          # connect + loop_start; retried by the bridge until it succeeds
        def publish(self, topic: str, payload, qos: int = 0, retain: bool = False) -> None
        def publish_device_state(self, device: Device, online: bool) -> None   # "<prefix><dev>/connected", qos 1, retain, via the device's client
        def stop(self) -> None           # publishes offline (global + per device), loop_stop, disconnect
    ```
  - **`modbus2mqtt/bridge.py`:** `class Bridge(args, config: Config, link: ModbusLink, mqtt: MqttSide)`, which has:
    - `async def run(self, stop: asyncio.Event) -> None`
    - `async def poll(self, poller: Poller) -> None`
    - `async def handle_message(self, msg) -> None`
    - `def record_result(self, poller: Poller, ok: bool) -> None`
  - **`modbus2mqtt/cli.py`:**
    - `configure_logging(verbosity: int) -> int` returns the root level: `≤0→CRITICAL, 1→WARNING, 2→INFO, 3→INFO, ≥4→DEBUG`. The `tmodbus` logger is set to WARNING unless verbosity ≥4.
    - `run(argv)` wires up config → link → MQTT → Bridge.
    - `run(argv)` installs SIGINT/SIGTERM through `loop.add_signal_handler`, which sets `stop` **and cancels the bridge task**, so a request blocked in reconnect is interrupted.
    - `run(argv)` always calls `mqtt.stop()` and `link.close()` in `finally`.

Behaviour rules for `Bridge`, carried over from today unless noted:
- **Main loop:**
  - Wait until `mqtt.initial_connection_made`.
  - Each iteration: service one due poller (round-robin, `next_due`/`rate`, initial random offset), then at most one inbox message, then diagnostics.
  - When every poller is disabled, re-enable all of them after 10 ms.
  - Then `await asyncio.sleep(loop_break)`, where `--set-loop-break` defaults to 0.01.
- **`LinkDown`** from `poll` or a write:
  - Don't call `record_result`, and push the in-flight `set` message back to the front of the queue so it isn't lost (keep a local `deque` ahead of `inbox`).
  - Log "MODBUS connection lost" once per outage.
  - `await asyncio.sleep(1)`, then continue. The next request triggers tmodbus's lazy reconnect.
- **`record_result`:** the `failCount` semantics from `modbus2mqtt.py:157-190` exactly. That covers:
  - the 2-strike `offline` and the immediate `online` on success
  - with `--autoremove`, disabling every poller on the same unit
  - error/poll counters for diagnostics
- **`handle_message`:**
  - `homeassistant/status` `online` republishes discovery.
  - `reset-autoremove`.
  - `<prefix><dev>/set/<ref>` (ref may contain `/`).
  - `<ref>/timezone` follows `_handle_tz_set` (`modbus2mqtt.py:354-390`).
  - Invalid UTF-8 or an unparseable value is logged and dropped.
  - Writes call `link.write(..., force_multiple=args.avoid_fc6)`. On success they publish via the same change-detection path as polling (`lastval`, scaling, `--always-publish`).
- **Publishing:** state goes to `<prefix><dev>/state/<topic>` with `retain=True`, QoS 0, only after `initial_connection_made`.

- [ ] **Step 1: Add the failing tests**
  - In `tests/test_bridge_e2e.py`, **`test_sigterm_during_reconnect_exits_promptly`** (Review Focus 2):
    - After online, `await device.stop()` and wait 0.2 s so a request is blocked in reconnect.
    - `signal.raise_signal(SIGTERM)`. The bridge task finishes within 3 s.
    - `broker.last("modbus/connected").payload == b"offline"`.
  - In `tests/test_cli.py`, **`test_verbosity_mapping`** (Review Focus 5): `configure_logging(n)` for `-1, 0, 1, 2, 3, 4, 5` gives `CRITICAL, CRITICAL, WARNING, INFO, INFO, DEBUG, DEBUG`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest -k "sigterm_during_reconnect or verbosity_mapping" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**
  - Implement `mqtt.py`, `bridge.py`, the new `cli.run` and `homeassistant.py`.
  - Delete `modbus2mqtt.py` and `_reset_legacy_globals`.
  - Drop pymodbus/pyserial and re-lock.
  - Confirm there are no `time.sleep` calls left and no `print` calls left: `grep -rn "time\.sleep\|print(" modbus2mqtt/` returns nothing.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass, with every Task 3 characterization test unmodified (except deleting the reset helper call). Also check that `uv tree` shows no `pymodbus` and no `pyserial`.

- [ ] **Step 5: Check the line budget**

Run: `wc -l modbus2mqtt/*.py | tail -1`
Expected: a total below 1329. If it isn't, simplify before committing.

- [ ] **Step 6: Commit** — `refactor: bridge/mqtt/cli modules on tmodbus; drop pymodbus and globals`

---

### Task 8: Image check, docs, release prep

**Files:**
- Modify: `README.md` (install/run via uv and Docker, `--rtu` help is now "serial port path", v2.0.0 changelog entry listing tmodbus, uv, tests, the float-scaling fix and logging-level mapping), `modbus2mqtt/cli.py` (`--rtu` help text only)

- [ ] **Step 1: Build the image**

Run: `docker build -t m2m:test .`
Expected: success. Then check:
- `docker run --rm m2m:test python3 -u modbus2mqtt.py --help` exits 0.
- `docker run --rm m2m:test python3 -c "import pymodbus"` fails with `ModuleNotFoundError`.
- `docker run --rm m2m:test python3 -c "import serialx, tmodbus"` exits 0.

- [ ] **Step 2: Update the README and the `--rtu` help text, then run the suite again**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 3: Commit and open the PR**

```bash
git add -A && git commit -m "docs: v2 README and changelog"
git push -u origin tmodbus-migration
gh pr create --title "Migrate to tmodbus, restructure, add tests, package with uv" --body "<summary + hardware test checklist>"
```

The PR body includes a hardware checklist for the owner. On the real RTU bus with the `v2.0.0-beta.2` image:
- Values publish.
- A write from HA lands.
- Unplugging and replugging the USB adapter leaves devices online and resumes polling.
- `docker stop` publishes offline.
- HA entities are unchanged, with no duplicates.

The owner tags `v2.0.0-beta.2` and later `v2.0.0`. Tagging isn't part of this plan.
