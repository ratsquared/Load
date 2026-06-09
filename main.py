from machine import Timer
import gc
import network
import socket
import time
import urequests

import all2_adjgain as load_controller

try:
    import _thread
except ImportError:
    _thread = None


# --------------------------------------------------------------------------------------
# Pico W Wi-Fi and API collection setup
#
# api.yaml defines:
#   GET /power/load_pwr -> {"load_pwr": number}
#
# U# If you host the thing locally u adjust WIFI and API_BASE_URL to match

WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

API_BASE_URL = "https://icelec50015.azurewebsites.net"
LOAD_POWER_PATH = "/power/load_pwr"
API_POLL_SECONDS = 0.2
API_RETRY_SECONDS = 2
API_TIMEOUT_SECONDS = 5


last_collected_data = None
pending_collected_data = None

if _thread:
    pending_data_lock = _thread.allocate_lock()
else:
    pending_data_lock = None


def load_power_url():
    return API_BASE_URL.rstrip("/") + LOAD_POWER_PATH


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


def set_api_timeout():
    try:
        socket.setdefaulttimeout(API_TIMEOUT_SECONDS)
    except AttributeError:
        pass


def fetch_load_power_data():
    response = None
    try:
        response = urequests.get(load_power_url())
        data = response.json()
    finally:
        if response is not None:
            response.close()
        gc.collect()

    if isinstance(data, dict):
        load_pwr = data.get("load_pwr")
    else:
        load_pwr = data

    if load_pwr is None:
        raise ValueError("API response missing load_pwr")

    return {"load_pwr": float(load_pwr)}


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
    load_controller.set_target_power_from_serial(float(data["load_pwr"]))


def collect_data():
    global last_collected_data

    set_api_timeout()
    first_run = True

    while True:
        try:
            data = fetch_load_power_data()

            if first_run or data != last_collected_data:
                first_run = False
                last_collected_data = data
                print("Collected load_pwr:", data["load_pwr"])
                store_collected_data(data)

            time.sleep(API_POLL_SECONDS)

        except Exception as error:
            print("Collector error:", error)
            time.sleep(API_RETRY_SECONDS)


def collect_data_step():
    global last_collected_data

    try:
        data = fetch_load_power_data()
        if data != last_collected_data:
            last_collected_data = data
            print("Collected load_pwr:", data["load_pwr"])
            process(data)
    except Exception as error:
        print("Collector error:", error)


def start_collector():
    if _thread:
        _thread.start_new_thread(collect_data, ())
        return

    print("No _thread module; collecting API data in the main loop.")


def main():
    connect_wifi()
    set_api_timeout()
    start_collector()

    load_controller.all_off()
    print("Pico W LED load controller ready.")
    print("API endpoint:", load_power_url())
    print("Max demo power is {:.2f} W.".format(load_controller.MAX_REQUESTED_LOAD_POWER_W))
    print("Use p=..., i=..., d=... to update PID gains.")
    print("Type 'gains', 'off' for 0 W, or 'reset' after a fault.")
    load_controller.print_pid_gains()

    load_controller.loop_timer = Timer(
        mode=Timer.PERIODIC,
        freq=load_controller.CONTROL_LOOP_HZ,
        callback=load_controller.tick,
    )
    last_main_collection = time.time()

    while True:
        load_controller.poll_serial_input()

        data = pop_collected_data()
        if data is not None:
            process(data)

        if _thread is None and time.time() - last_main_collection >= API_POLL_SECONDS:
            last_main_collection = time.time()
            collect_data_step()

        if load_controller.timer_elapsed == 1:
            load_controller.timer_elapsed = 0
            load_controller.control_step()

        time.sleep_us(50)


try:
    main()
finally:
    load_controller.all_off()
