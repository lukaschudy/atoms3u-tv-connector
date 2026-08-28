#include <Arduino.h>
#include <USB.h>
#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>

USBHIDKeyboard diagnosticKeyboard;
USBHIDConsumerControl diagnosticConsumer;

void setup() {
  diagnosticKeyboard.begin();
  diagnosticConsumer.begin();
  USB.productName("Room Agent HID Diagnostic");
  USB.manufacturerName("Room Agent");
  USB.begin();
}

void loop() {
  delay(1000);
}
