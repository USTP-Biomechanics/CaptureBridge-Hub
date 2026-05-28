// Arduino bridge for tcp_arduino_sync.py
//
// PC -> Arduino:
//   '1' or "START" drives D10 HIGH
//   '0' or "STOP"  drives D10 LOW
//   "PING"         replies with a bridge identifier for safe port detection
//
// Arduino -> PC:
//   Sends "START\n" when A0 rises above START_THRESHOLD
//   Sends "STOP\n"  when A0 falls below STOP_THRESHOLD
//   Sends "CAPTUREBRIDGE_ARDUINO_BRIDGE\n" when probed with "PING"
//
// The Python app listens at 9600 baud and accepts either 1/0 or START/STOP.

constexpr byte OUTPUT_PIN = 10;
constexpr byte ANALOG_INPUT_PIN = A0;
constexpr unsigned long SERIAL_BAUD = 9600;

// Tune these values for your incoming analog signal.
constexpr int START_THRESHOLD = 700;
constexpr int STOP_THRESHOLD = 300;

// Basic debounce for noisy analog edges.
constexpr unsigned long SAMPLE_INTERVAL_MS = 5;
constexpr unsigned long STATE_DEBOUNCE_MS = 25;

bool outputState = false;
bool inputState = false;
bool pendingInputState = false;
bool hasPendingInputChange = false;

unsigned long lastSampleMs = 0;
unsigned long pendingInputSinceMs = 0;

String serialLine;

void applyOutputState(bool active) {
  outputState = active;
  digitalWrite(OUTPUT_PIN, active ? HIGH : LOW);
}

void sendInputState(bool active) {
  Serial.println(active ? F("START") : F("STOP"));
}

bool classifyInputState(int sample, bool currentState) {
  if (sample >= START_THRESHOLD) {
    return true;
  }
  if (sample <= STOP_THRESHOLD) {
    return false;
  }
  return currentState;
}

void handleSerialCommand(const String& command) {
  if (command == F("1") || command == F("START")) {
    applyOutputState(true);
  } else if (command == F("0") || command == F("STOP")) {
    applyOutputState(false);
  } else if (command == F("PING")) {
    Serial.println(F("CAPTUREBRIDGE_ARDUINO_BRIDGE"));
  }
}

void handleIncomingSerial() {
  while (Serial.available() > 0) {
    char raw = static_cast<char>(Serial.read());

    if (raw == '1') {
      applyOutputState(true);
      serialLine = "";
      continue;
    }
    if (raw == '0') {
      applyOutputState(false);
      serialLine = "";
      continue;
    }

    if (raw == '\r' || raw == '\n') {
      if (serialLine.length() > 0) {
        serialLine.toUpperCase();
        handleSerialCommand(serialLine);
        serialLine = "";
      }
      continue;
    }

    if (isPrintable(raw)) {
      if (serialLine.length() < 16) {
        serialLine += raw;
      } else {
        serialLine = "";
      }
    }
  }
}

void updateAnalogInput() {
  unsigned long now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) {
    return;
  }
  lastSampleMs = now;

  int sample = analogRead(ANALOG_INPUT_PIN);
  bool candidateState = classifyInputState(sample, inputState);

  if (candidateState == inputState) {
    hasPendingInputChange = false;
    return;
  }

  if (!hasPendingInputChange || pendingInputState != candidateState) {
    pendingInputState = candidateState;
    pendingInputSinceMs = now;
    hasPendingInputChange = true;
    return;
  }

  if (now - pendingInputSinceMs >= STATE_DEBOUNCE_MS) {
    inputState = pendingInputState;
    hasPendingInputChange = false;
    sendInputState(inputState);
  }
}

void setup() {
  pinMode(OUTPUT_PIN, OUTPUT);
  pinMode(ANALOG_INPUT_PIN, INPUT);
  applyOutputState(false);

  Serial.begin(SERIAL_BAUD);

  int initialSample = analogRead(ANALOG_INPUT_PIN);
  int midpoint = (START_THRESHOLD + STOP_THRESHOLD) / 2;
  inputState = initialSample >= midpoint;
  pendingInputState = inputState;
}

void loop() {
  handleIncomingSerial();
  updateAnalogInput();
}
