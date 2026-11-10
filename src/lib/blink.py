from machine import Pin
import time


def blink(pin: int = 2, times: int = 3, delay_ms: int = 200) -> None:
    led = Pin(pin, Pin.OUT)
    for _ in range(times):
        led.on()
        time.sleep_ms(delay_ms)
        led.off()
        time.sleep_ms(delay_ms)
