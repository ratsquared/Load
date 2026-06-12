#this code to test with imperial website
#update: persistent HTTPS connection — TLS handshake paid once, reused every poll
import machine
from machine import Timer
import gc
import json
import network
import socket
import ssl
import time

import all2_adjgain as load_controller

try:
    import _thread
except ImportError:
    _thread = None


# --------------------------------------------------------------------------------------
# Pico W Wi-Fi and API collection setup

WIFI_SSID = "TT"
WIFI_PASSWORD = "123456789"

API_HOST = "icelec50015.azurewebsites.net"
LOAD_POWER_PATH = "/demand"
API_TIMEOUT_SECONDS = 5
API_RETRY_MS = 200
FAULT_AUTO_RESET_SECONDS = 2
FAULT_AUTO_RESET_MS = int(FAULT_AUTO_RESET_SECONDS * 1000)


# --------------------------------------------------------------------------------------
# Persistent HTTPS client
#
# Opens one TCP+TLS connection, keeps it alive, and reuses it for every GET.
# On any socket error (server timeout, connection dropped, etc.) it closes the
# socket and transparently reconnects on the next request — paying the TLS
# handshake cost only on reconnect, not on every poll.

class PersistentHTTPSClient:
    def __init__(self, host, timeout=API_TIMEOUT_SECONDS):
        self.host = host
        self.timeout = timeout
        self._sock = None

    def _connect(self):
        self._close()
        addr = socket.getaddrinfo(self.host, 443, 0, socket.SOCK_STREAM)[0][-1]
        raw = socket.socket()
        raw.settimeout(self.timeout)
        raw.connect(addr)
        # TLS handshake happens here — paid once per connection lifetime
        self._sock = ssl.wrap_socket(raw, server_hostname=self.host)
        print("HTTPS connected (TLS handshake done)")

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def get_json(self, path):
        # On stale/broken connection, reconnect once and retry automatically
        for attempt in range(2):
            try:
                if self._sock is None:
                    self._connect()
                return self._request(path)
            except Exception as e:
                print("HTTPS error (attempt {}): {}".format(attempt + 1, e))
                self._close()
                if attempt == 1:
                    raise

    def _request(self, path):
        req = (
            "GET {} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "Connection: keep-alive\r\n"
            "Accept: application/json\r\n"
            "\r\n"
        ).format(path, self.host).encode()

        self._sock.write(req)

        # --- parse status line ---
        status_line = self._readline()
        parts = status_line.split()
        if len(parts) < 2:
            raise RuntimeError("Bad HTTP status: " + status_line)
        status_code = int(parts[1])
        if status_code != 200:
            raise RuntimeError("HTTP {}".format(status_code))

        # --- parse headers ---
        content_length = -1
        chunked = False
        server_wants_close = False
        while True:
            line = self._readline()
            if not line:
                break
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k == "content-length":
                content_length = int(v)
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
            elif k == "connection" and "close" in v.lower():
                server_wants_close = True

        # --- read body ---
        if chunked:
            body = self._read_chunked()
        elif content_length >= 0:
            body = self._readn(content_length)
        else:
            body = self._sock.read(512)

        if server_wants_close:
            self._close()

        gc.collect()
        return json.loads(body)

    def _readline(self):
        line = bytearray()
        while True:
            ch = self._sock.read(1)
            if not ch:
                raise RuntimeError("Connection closed mid-header")
            if ch == b"\n":
                return line.decode().rstrip("\r")
            line += ch

    def _readn(self, n):
        buf = bytearray(n)
        view = memoryview(buf)
        received = 0
        while received < n:
            chunk = self._sock.read(n - received)
            if not chunk:
                raise RuntimeError("Connection closed mid-body")
            view[received:received + len(chunk)] = chunk
            received += len(chunk)
        return bytes(buf)

    def _read_chunked(self):
        body = bytearray()
        while True:
            size_line = self._readline().strip()
            size = int(size_line, 16)
            if size == 0:
                self._readline()  # consume trailing CRLF
                break
            body += self._readn(size)
            self._readline()  # consume CRLF after chunk data
        return bytes(body)

    def close(self):
        self._close()


# --------------------------------------------------------------------------------------
# Global state

last_collected_data = None
pending_collected_data = None
_https_client = PersistentHTTPSClient(API_HOST)

if _thread:
    pending_data_lock = _thread.allocate_lock()
else:
    pending_data_lock = None


def now_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.time() * 1000)


def elapsed_ms(start_ms):
    try:
        return time.ticks_diff(now_ms(), start_ms)
    except AttributeError:
        return now_ms() - start_ms


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print("Wi-Fi already connected:", wlan.ifconfig())
        return wlan

    print("Connecting Wi-Fi...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    deadline = time.time() + 20
    while not wlan.isconnected():
        if time.time() >= deadline:
            raise RuntimeError("Wi-Fi connection timed out")
        time.sleep(0.25)

    print("Wi-Fi connected:", wlan.ifconfig())
    return wlan


def fetch_load_power_data():
    data = _https_client.get_json(LOAD_POWER_PATH)

    if isinstance(data, dict):
        # load_pwr = data.get("load_pwr")
        measured_power = min (data.get("demand"), 10)
        load_pwr = (measured_power - 0.31812127) / 1.3382855
    else:
        load_pwr = data
        print(load_pwr, "unexpected response format")

    if load_pwr is None:
        raise ValueError("API response missing 'demand'")

    return {"demand": float(load_pwr)}


def store_collected_data(data):
    global pending_collected_data
    if pending_data_lock:
        pending_data_lock.acquire()
        pending_collected_data = data
        pending_data_lock.release()
    else:
        pending_collected_data = data


def pop_collected_data():
    global pending_collected_data
    if pending_data_lock:
        pending_data_lock.acquire()
        data = pending_collected_data
        pending_collected_data = None
        pending_data_lock.release()
        return data
    data = pending_collected_data
    pending_collected_data = None
    return data


def process(data):
    print("demand:", float(data["demand"]))
    load_controller.set_target_power_from_serial(float(data["demand"]))


def collect_data():
    global last_collected_data
    first_run = True

    while True:
        try:
            data = fetch_load_power_data()

            if first_run or data != last_collected_data:
                first_run = False
                last_collected_data = data
                print("Collected demand:", data["demand"])
                store_collected_data(data)

        except Exception as error:
            print("Collector error:", error)
            wlan = network.WLAN(network.STA_IF)
            if not wlan.isconnected():
                print("WiFi lost, reconnecting...")
                try:
                    connect_wifi()
                except Exception as wifi_error:
                    print("WiFi reconnect failed:", wifi_error)
            time.sleep_ms(API_RETRY_MS)


def collect_data_step():
    global last_collected_data
    try:
        data = fetch_load_power_data()
        if data != last_collected_data:
            last_collected_data = data
            print("Collected demand:", data["demand"])
            process(data)
    except Exception as error:
        print("Collector error:", error)


def handle_fault_auto_reset(fault_started_ms):
    if load_controller.fault_latched:
        if fault_started_ms is None:
            return now_ms()
        if elapsed_ms(fault_started_ms) >= FAULT_AUTO_RESET_MS:
            print("Auto-resetting fault after {:.1f} seconds.".format(FAULT_AUTO_RESET_SECONDS))
            load_controller.clear_fault()
            return None
        return fault_started_ms
    return None


def start_collector():
    if _thread:
        _thread.start_new_thread(collect_data, ())
        return
    print("No _thread module; collecting API data in the main loop.")


def main():
    connect_wifi()
    start_collector()

    load_controller.all_off()
    print("Pico W LED load controller ready.")
    print("API host:", API_HOST + LOAD_POWER_PATH)
    print("Max demo power is {:.2f} W.".format(load_controller.MAX_REQUESTED_LOAD_POWER_W))
    print("Use p=..., i=..., d=... to update PID gains.")
    print("Type 'gains', 'off' for 0 W, or 'reset' after a fault.")
    load_controller.print_pid_gains()

    load_controller.loop_timer = Timer(
        mode=Timer.PERIODIC,
        freq=load_controller.CONTROL_LOOP_HZ,
        callback=load_controller.tick,
    )
    last_main_collection_ms = now_ms()
    fault_started_ms = None

    while True:
        load_controller.poll_serial_input()

        data = pop_collected_data()
        if data is not None:
            process(data)

        if _thread is None and elapsed_ms(last_main_collection_ms) >= 200:
            last_main_collection_ms = now_ms()
            collect_data_step()

        fault_started_ms = handle_fault_auto_reset(fault_started_ms)

        if load_controller.timer_elapsed == 1:
            load_controller.timer_elapsed = 0
            load_controller.control_step()

        time.sleep_us(50)


try:
    main()
except KeyboardInterrupt:
    # Thonny Stop button — clean shutdown, no reset
    _https_client.close()
    load_controller.all_off()
except Exception as e:
    # Any other fatal error — reset both cores cleanly
    print("Fatal error:", e)
    try:
        _https_client.close()
        load_controller.all_off()
    except Exception:
        pass
    time.sleep_ms(500)
    machine.reset()
