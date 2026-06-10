from machine import Pin, PWM, Timer, SPI
from PID import PID  # Copy PID.py to the Pico W as well.
import select
import sys
import time


# --------------------------------------------------------------------------------------
# Serial input setup
#
# Type a power value in watts over the Pico USB serial connection, then press Enter.
# Examples:
#   2.5
#   p=0.005
#   i=0.2
#   d=0
#   0
#   off
#   reset
#   gains

SERIAL_BUFFER_MAX_CHARS = 32


# --------------------------------------------------------------------------------------
# Hardware/scaling setup

ADC_REF_V = 2.497
ADC_COUNTS = 4096.0
CURRENT_SENSE_OHMS = 0.33
VOLTAGE_DIVIDER_RATIO = 2.0

# Adjustable safety/current limits.
MAX_LED_CURRENT_A = 0.8
OVERCURRENT_SHUTDOWN_A = 1.30
MAX_LED_VOLTAGE_V = 6.0
MIN_VALID_LED_VOLTAGE_V = 0.05
MIN_CURRENT_SETPOINT_A = 0.01

# Used when an LED is off and its measured voltage is too low to divide power by.
# Adjust these to match your LED board.
NOMINAL_BLUE_V = 3.0
NOMINAL_GREEN_V = 3.0
NOMINAL_RED_V = 2.0

MAX_REQUESTED_LOAD_POWER_W = (
    MAX_LED_CURRENT_A * NOMINAL_BLUE_V
    + MAX_LED_CURRENT_A * NOMINAL_GREEN_V
    + MAX_LED_CURRENT_A * NOMINAL_RED_V
)

PWM_FREQ_HZ = 100000
PWM_MAX_DUTY = 62500
PWM_MIN_ACTIVE_DUTY = 0

CONTROL_LOOP_HZ = 10000
# Reduced from 100 (10ms / 100 Hz) to 1000 (100ms / 10 Hz).
# 100 lines/s over USB serial was clogging the serial buffer.
PRINT_PERIOD_TICKS = 1000


# --------------------------------------------------------------------------------------
# Global state

timer_elapsed = 0
target_load_power_w = 0.0
unmatched_power_w = 0.0
fault_latched = False
fault_reason = ""
print_counter = 0
serial_buffer = ""
loop_timer = None


def tick(timer):
    global timer_elapsed
    timer_elapsed = 1


# --------------------------------------------------------------------------------------
# LED control setup


class LedChannel:
    def __init__(
        self,
        name,
        pwm_pin,
        enable_pin,
        current_adc_channel,
        voltage_adc_channel,
        nominal_voltage,
    ):
        self.name = name
        self.enable = Pin(enable_pin, Pin.OUT)
        self.pwm = PWM(Pin(pwm_pin))
        self.pwm.freq(PWM_FREQ_HZ)
        self.current_adc_channel = current_adc_channel
        self.voltage_adc_channel = voltage_adc_channel
        self.nominal_voltage = nominal_voltage
        self.current_a = 0.0
        self.voltage_v = nominal_voltage
        self.current_setpoint_a = 0.0
        self.controller = PID(0.001, 1, 0, setpoint=0.0, scale="ms")

    def off(self):
        self.current_setpoint_a = 0.0
        self.controller.setpoint = 0.0
        self.controller.reset()
        self.pwm.duty_u16(0)
        self.enable.value(0)

    def enable_if_needed(self):
        self.enable.value(1 if self.current_setpoint_a > 0 else 0)


# The original led.py calls this channel "yellow". Here it is used as blue.
blue = LedChannel(
    "blue",
    pwm_pin=9,
    enable_pin=8,
    current_adc_channel=2,
    voltage_adc_channel=3,
    nominal_voltage=NOMINAL_BLUE_V,
)
green = LedChannel(
    "green",
    pwm_pin=7,
    enable_pin=6,
    current_adc_channel=0,
    voltage_adc_channel=1,
    nominal_voltage=NOMINAL_GREEN_V,
)
red = LedChannel(
    "red",
    pwm_pin=11,
    enable_pin=10,
    current_adc_channel=4,
    voltage_adc_channel=5,
    nominal_voltage=NOMINAL_RED_V,
)

channels = [blue, green, red]

# Raised SPI baudrate from 400 kHz to 1 MHz.
# MCP3208 is rated to 1 MHz at Vdd=2.7 V; at 3.3 V this is comfortably within spec.
# Each 3-byte transaction drops from ~60 µs to ~24 µs; 6 reads/step saves ~216 µs,
# raising the effective control rate from ~2.4 kHz to closer to 10 kHz.
spi = SPI(0, baudrate=1000000)
adc_cs = Pin(17, mode=Pin.OUT, value=1)

serial_poll = select.poll()
serial_poll.register(sys.stdin, select.POLLIN)


def read_adc(channel):
    txdata = bytearray([6 + (channel >> 2), (channel & 3) << 6, 0])
    rxdata = bytearray(len(txdata))

    try:
        adc_cs(0)
        # CS setup time: MCP3208 requires 100 ns; 2 µs is well within spec
        # and saves 8 µs vs the previous 10 µs (×6 reads = 48 µs/step).
        time.sleep_us(2)
        spi.write_readinto(txdata, rxdata)
    finally:
        adc_cs(1)

    value = ((rxdata[1] & 15) << 8) + rxdata[2]
    if value < 0 or value > 4095:
        raise RuntimeError("ADC reading out of range")
    return value


def adc_to_voltage(adc_value):
    return ADC_REF_V * (adc_value / ADC_COUNTS)


def all_off():
    for channel in channels:
        channel.off()


def set_fault(reason):
    global fault_latched, fault_reason
    fault_latched = True
    fault_reason = reason
    all_off()
    print("FAULT:", reason)
    print("Type 'reset' over serial to clear the latched fault.")


def clear_fault():
    global fault_latched, fault_reason
    fault_latched = False
    fault_reason = ""
    all_off()
    print("Fault cleared. Target remains {:.2f} W.".format(target_load_power_w))


def clamp(value, minimum, maximum):
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def saturate_duty(duty, setpoint):
    if setpoint <= 0:
        return 0
    if duty > PWM_MAX_DUTY:
        return PWM_MAX_DUTY
    if duty < PWM_MIN_ACTIVE_DUTY:
        return PWM_MIN_ACTIVE_DUTY
    return duty


def measure_channel(channel):
    current_pin_v = adc_to_voltage(read_adc(channel.current_adc_channel))
    voltage_pin_v = adc_to_voltage(read_adc(channel.voltage_adc_channel))

    current_a = current_pin_v / CURRENT_SENSE_OHMS
    led_voltage_v = VOLTAGE_DIVIDER_RATIO * voltage_pin_v - current_pin_v

    if current_a < -0.05:
        raise RuntimeError(channel.name + " current measurement invalid")
    if led_voltage_v < -0.20 or led_voltage_v > MAX_LED_VOLTAGE_V:
        raise RuntimeError(channel.name + " voltage measurement invalid")
    if current_a > OVERCURRENT_SHUTDOWN_A:
        raise RuntimeError(channel.name + " overcurrent shutdown")

    channel.current_a = max(0.0, current_a)
    channel.voltage_v = max(0.0, led_voltage_v)


def measure_all_channels():
    for channel in channels:
        measure_channel(channel)


def voltage_for_power_allocation(channel):
    if channel.voltage_v >= MIN_VALID_LED_VOLTAGE_V:
        return channel.voltage_v
    return channel.nominal_voltage


def allocate_current_setpoints(load_power_w):
    global unmatched_power_w, MIN_CURRENT_SETPOINT_A

    remaining_power = clamp(load_power_w, 0.0, MAX_REQUESTED_LOAD_POWER_W)
    for i in range(len(channels)):
        channel = channels[i]
        voltage = voltage_for_power_allocation(channel)
        current_setpoint = remaining_power / voltage
        if current_setpoint <= 0:
            channel.current_setpoint_a = 0
        elif current_setpoint >= MIN_CURRENT_SETPOINT_A:
            current_setpoint = clamp(current_setpoint, MIN_CURRENT_SETPOINT_A, MAX_LED_CURRENT_A)
            channel.current_setpoint_a = current_setpoint
        else:  #current_setpoint < MIN_CURRENT_SETPOINT_A:
            if i == 0:
                channel.current_setpoint_a = current_setpoint
            else:
                prevchannel = channels[i-1]
                prev_voltage = voltage_for_power_allocation(prevchannel)
                this_channel_power = current_setpoint * voltage
                prev_channel_power = prevchannel.current_setpoint_a * prev_voltage
                new_prev_channel_power = prev_channel_power + this_channel_power - MIN_CURRENT_SETPOINT_A * voltage

                channel.current_setpoint_a = MIN_CURRENT_SETPOINT_A
                prevchannel.current_setpoint_a = new_prev_channel_power / prev_voltage
                prevchannel.controller.setpoint = prevchannel.current_setpoint_a

        channel.controller.setpoint = channel.current_setpoint_a
        remaining_power -= current_setpoint * voltage
        if remaining_power < 0:

            remaining_power = 0.0

    unmatched_power_w = remaining_power


def update_pwm_outputs():
    for channel in channels:
        if channel.current_setpoint_a <= 0:
            channel.off()
            continue

        channel.enable.value(1)

        pwm_ref = channel.controller(channel.current_a)
        if pwm_ref is None:
            pwm_ref = 0.0

        duty = saturate_duty(int(pwm_ref * 65536), channel.current_setpoint_a)
        channel.pwm.duty_u16(duty)


def measured_total_power():
    total = 0.0
    for channel in channels:
        total += channel.voltage_v * channel.current_a
    return total


def set_target_power_from_serial(value_w):
    global target_load_power_w

    if value_w < 0:
        value_w = 0.0

    if value_w > MAX_REQUESTED_LOAD_POWER_W:
        print(
            "Requested {:.2f} W is too high; clamping to {:.2f} W.".format(
                value_w,
                MAX_REQUESTED_LOAD_POWER_W,
            )
        )
        value_w = MAX_REQUESTED_LOAD_POWER_W

    target_load_power_w = value_w
    print("Target load power set to {:.2f} W".format(target_load_power_w))


def print_pid_gains():
    kp, ki, kd = blue.controller.tunings
    print("PID gains: p={:.6f}, i={:.6f}, d={:.6f}".format(kp, ki, kd))


def set_pid_gain(gain_name, value):
    if value < 0:
        print("PID gain must be non-negative.")
        return

    kp, ki, kd = blue.controller.tunings

    if gain_name == "p" or gain_name == "kp":
        kp = value
    elif gain_name == "i" or gain_name == "ki":
        ki = value
    elif gain_name == "d" or gain_name == "kd":
        kd = value
    else:
        print("Unknown PID gain. Use p=..., i=..., or d=...")
        return

    for channel in channels:
        channel.controller.tunings = (kp, ki, kd)
        channel.controller.reset()

    print_pid_gains()


def handle_pid_gain_command(line):
    parts = line.split("=", 1)
    if len(parts) != 2:
        return False

    gain_name = parts[0].strip()
    value_text = parts[1].strip()

    try:
        gain_value = float(value_text)
    except ValueError:
        print("Invalid PID gain value. Example: p=0.005")
        return True

    set_pid_gain(gain_name, gain_value)
    return True


def handle_serial_line(line):
    global target_load_power_w

    line = line.strip().lower()
    if not line:
        return

    if handle_pid_gain_command(line):
        return

    if line == "off":
        target_load_power_w = 0.0
        all_off()
        print("Target load power set to 0 W")
        return

    if line == "reset":
        clear_fault()
        return

    if line == "gains":
        print_pid_gains()
        return

    try:
        set_target_power_from_serial(float(line))
    except ValueError:
        print("Invalid input. Type watts, p=..., i=..., d=..., 'gains', 'off', or 'reset'.")


def poll_serial_input():
    global serial_buffer
    while serial_poll.poll(0):
        char = sys.stdin.read(1)
        if not char:
            return

        if char == "\r" or char == "\n":
            line = serial_buffer
            serial_buffer = ""
            handle_serial_line(line)
        elif len(serial_buffer) < SERIAL_BUFFER_MAX_CHARS:
            serial_buffer += char
        else:
            serial_buffer = ""
            print("Serial input too long; buffer cleared.")


def control_step():
    global print_counter

    if fault_latched:
        all_off()
        return

    try:
        measure_all_channels()
        allocate_current_setpoints(target_load_power_w)
        update_pwm_outputs()
    except Exception as error:
        set_fault(str(error))
        return

    print_counter += 1
    if print_counter >= PRINT_PERIOD_TICKS:
        print_counter = 0
        print(
            "target={:.2f}W measured={:.2f}W unmatched={:.2f}W | "
            "Iblue={:.3f}/{:.3f} Igreen={:.3f}/{:.3f} Ired={:.3f}/{:.3f}".format(
                target_load_power_w,
                measured_total_power(),
                unmatched_power_w,
                blue.current_a,
                blue.current_setpoint_a,
                green.current_a,
                green.current_setpoint_a,
                red.current_a,
                red.current_setpoint_a,
            )
        )


def main():
    global timer_elapsed, loop_timer

    all_off()
    print("Serial LED load controller ready.")
    print("Type target power in watts, then press Enter.")
    print("Max demo power is {:.2f} W.".format(MAX_REQUESTED_LOAD_POWER_W))
    print("Use p=..., i=..., d=... to update PID gains.")
    print("Type 'gains', 'off' for 0 W, or 'reset' after a fault.")
    print_pid_gains()

    loop_timer = Timer(mode=Timer.PERIODIC, freq=CONTROL_LOOP_HZ, callback=tick)

    while True:
        poll_serial_input()

        if timer_elapsed == 1:
            timer_elapsed = 0
            control_step()

        time.sleep_us(50)


if __name__ == "__main__":
    main()
