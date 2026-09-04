# LoRa Walkie-Talkie

Push-to-talk voice and text over LoRa, on a Raspberry Pi Zero 2 W with a
**Whisplay HAT** (240×280 LCD, one button, RGB LED, audio codec) and a
**Waveshare SX126X LoRa HAT** (E22-900T22S).

Hold the button to talk. Release and the clip is encoded, fragmented and
transmitted. Anything another station sends arrives and plays on its
own, like a walkie-talkie — no pairing handshake, no gateway, no
internet, a kilometre or more of range.

```
   hold          record, then send to the selected station
   1 click       next contact / next message / next screen
   2 clicks      open, play, or go back
   3 clicks      replay the last voice message
   4 clicks      leave the app
```

---

## How voice fits down a 700 bps pipe

This is the part that decides everything else about the design.

LoRa is not a voice channel. In EU 868 MHz, ETSI caps transmission at a
**1% duty cycle** — 36 seconds of airtime *per hour*. A normal speech
codec at 6–64 kbps would exhaust an hour's legal budget in a few
seconds, so a live audio stream is out of the question.

[Codec2](https://www.rowetel.com/codec2.html) changes the arithmetic. Its
700C mode encodes intelligible speech at **700 bps — 100 bytes per
second**:

| | 10 s of speech | LoRa packets | airtime @ 9600 bps | messages per hour @ 1% duty |
|---|---|---|---|---|
| Raw PCM 8 kHz | 160 kB | 830 | 140 s | 0 |
| Opus @ 16 kbps | 20 kB | 104 | 18 s | 2 |
| **Codec2 700C** | **1.0 kB** | **6** | **1.3 s** | **27** |
| Codec2 1300 | 1.6 kB | 9 | 2.1 s | 17 |

So the app is **store-and-forward**, not streaming: record → encode →
fragment → transmit → reassemble → decode → play. At these rates a
ten-second message takes about two seconds of air, and the duty cycle
holds a couple of dozen of them an hour. The app tracks that budget in a
rolling one-hour window and refuses to transmit past it
([app/radio/airtime.py](app/radio/airtime.py)).

---

## Two hardware facts that shape the build

**1. The two HATs collide on GPIO 22 and 27.** Those are the SX126X's
M0/M1 mode pins *and* two of the Whisplay LCD's control lines. The
display daemon claims them at boot:

```
$ gpioinfo | grep -E 'line +(22|27):'
line  22: "GPIO22"  output consumer="whisplay"
line  27: "GPIO27"  output consumer="whisplay"
```

The stock Waveshare driver grabs both pins on every start, because it
writes the module's *volatile* register bank (`0xC2`) and has to
reconfigure after each power-up. This app writes the **non-volatile**
bank (`0xC0`) once from [provision_radio.py](provision_radio.py), then
never touches GPIO: the module keeps its frequency, address and air rate
across power cycles and stays in transparent mode.

### Fixing the M0/M1 clash

Leaving the jumpers on is not an option: the LCD parks those lines
somewhere deaf, and the app will tell you so — *"THE RADIO IS DEAF:
module is in configuration mode (100% of the time)"* — because
everything above the radio otherwise reports success while nothing goes
on the air.

**Whichever option you pick, remove the two jumpers first.** Tying a pin
to ground while a GPIO is still driving it high shorts that output
through the pin and can damage the Pi.

**Option A — rewire to free GPIOs (recommended).** Two jumper wires from
the HAT's M0/M1 to spare pins, then:

```yaml
radio:
  mode_pins: [5, 6]      # M0 -> GPIO5 (pin 29), M1 -> GPIO6 (pin 31)
```

The app drives them low itself, and `provision_radio.py` picks the pins
up from this setting automatically. Free on both Pis here: **GPIO 5, 6,
12, 13, 16, 26**. Do not use 14/15 (the UART to this module), 2/3 (the
audio codec's I²C), 9/10/11 (the LCD's SPI) or 18–21 (I²S audio) — they
look unclaimed in `gpioinfo` but are in use.

**Option B — tie M0 and M1 to ground.** Both pins to any GND pin (6, 9,
14, 20, 25, 30, 34, 39) forces transparent mode permanently. Simpler,
and fine if the radio never needs reconfiguring again.

The catch is that configuration mode needs **M1 high**, so grounded pins
mean the module can never be reprovisioned without unsoldering — and
that includes changing **Device ID from the Settings screen**, which is
only half-applied until the module is reprovisioned to match. If you
expect to change addresses, frequency or air rate, take option A.

**2. A serial console will corrupt every transmission.** Raspberry Pi OS
puts a kernel console and a login prompt on `/dev/ttyS0` by default —
the same port the LoRa HAT uses. `setup.sh` detects and fixes this.

---

## Install

### The short way

```bash
git clone git@github.com:Mengkungkao/WalkieTalkie.git
cd WalkieTalkie
./setup.sh
```

> This repository is private, so a plain HTTPS clone onto a headless Pi
> will stop at `could not read Username for 'https://github.com'`.
> [docs/cloning-to-a-pi.md](docs/cloning-to-a-pi.md) covers the three
> ways through that — agent forwarding, a deploy key, or an account key
> — and the errors each one produces when it is the wrong choice.

`setup.sh` walks all eight steps below, asks before it changes anything,
and is safe to re-run. `--check` reports without changing; `--yes` runs
unattended.

From a development machine, push to the Pi and run it there:

```bash
./deploy.sh jarvis@192.168.0.33
ssh jarvis@192.168.0.33 'cd WalkieTalkie && ./setup.sh'
```

### The long way, step by step

<details open>
<summary><b>Step 1 — System packages</b></summary>

```bash
sudo apt update
sudo apt install -y python3-serial python3-yaml python3-pil python3-numpy \
                    libcodec2-1.2 alsa-utils python3-pytest
```

`libcodec2` is a **system shared library**, loaded with `ctypes` — there
is no Python wheel to build and no `-dev` package needed. Verify:

```bash
python3 -c "import ctypes.util; print(ctypes.util.find_library('codec2'))"
# libcodec2.so.1.2
```
</details>

<details open>
<summary><b>Step 2 — Enable the UART</b></summary>

The LoRa HAT talks over GPIO 14/15 (`/dev/ttyS0`), which is off by
default.

```bash
grep enable_uart /boot/firmware/config.txt || \
  echo "enable_uart=1" | sudo tee -a /boot/firmware/config.txt
```

Check whether the *running* firmware picked it up — editing the file
after boot changes nothing until you reboot:

```bash
vcgencmd get_config enable_uart     # want: enable_uart=1
cat /proc/cmdline | tr ' ' '\n' | grep nr_uarts   # want: 8250.nr_uarts=1
```

`8250.nr_uarts=0` means the firmware disabled the mini-UART and
`/dev/ttyS0` can never appear. **Reboot** and re-check.
</details>

<details open>
<summary><b>Step 3 — Get the login console off the LoRa port</b></summary>

This step is not optional. If `console=serial0` is in `cmdline.txt`, the
kernel writes its messages to the LoRa module at 115200 baud and
`agetty` sits on the port answering incoming packets with a login
prompt.

```bash
fuser -v /dev/ttyS0        # if agetty appears here, fix this now
```

Remove the console and disable the getty:

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo sed -i -E 's/console=(serial0|ttyS0),[0-9]+ ?//g' /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyS0.service
sudo reboot
```

Equivalently: `sudo raspi-config` → Interface Options → Serial Port →
login shell **No**, serial hardware **Yes**.

Note that `systemctl is-enabled serial-getty@ttyS0` can report
`disabled` while the service is still running: `systemd-getty-generator`
spawns a getty for whatever device `console=` names, regardless. Removing
the `console=` entry is what actually frees the port.

After the reboot:

```bash
ls -l /dev/serial0 /dev/ttyS0   # both should exist
fuser -v /dev/ttyS0             # should print nothing
groups | grep dialout           # or: sudo usermod -aG dialout $USER
```
</details>

<details open>
<summary><b>Step 4 — Remove the M0/M1 jumpers</b></summary>

Physically pull the two jumpers labelled **M0** and **M1** on the LoRa
HAT, so it stops driving GPIO 22 and 27 — the Whisplay LCD needs them.
See [the hardware section above](#two-hardware-facts-that-shape-the-build).
</details>

<details open>
<summary><b>Step 5 — Provision the radio (once per device)</b></summary>

Writing the module's settings needs M0/M1 for a few seconds, so stop the
display daemon first. Settings are stored in non-volatile memory and
survive power cycles, so this is done once, not at every start.

```bash
sudo systemctl stop whisplay-daemon
python3 provision_radio.py --address 5 --frequency 868
sudo systemctl start whisplay-daemon
```

**Every radio on the channel needs a different `--address`** (0–65534;
65535 is broadcast) and the **same `--frequency`**. Read back what a
module currently holds without changing it:

```bash
python3 provision_radio.py --check
```
</details>

<details open>
<summary><b>Step 6 — Configure the app</b></summary>

Edit [config.yaml](config.yaml) — at minimum your callsign, this node's
address, and the stations you want on the contacts screen:

```yaml
identity:
  callsign: Rover
radio:
  address: 5             # unique per radio
  frequency_mhz: 868
  duty_cycle_percent: 1.0  # ETSI EU868. Raise only where licensed.
contacts:
  - {name: Base,    address: 1}
  - {name: Hilltop, address: 2}
```

Stations not listed here still appear on the contacts screen
automatically once they transmit.
</details>

<details open>
<summary><b>Step 7 — Register with the Whisplay daemon</b></summary>

```bash
./install.sh                # adds "LoRa Walkie" to the HAT desktop
./install.sh --autostart    # ...and starts it at login
```

For autostart to survive a reboot without logging in:

```bash
sudo loginctl enable-linger $USER
```
</details>

<details open>
<summary><b>Step 8 — Run it</b></summary>

```bash
./run.sh
```

…or long-press to launch **LoRa Walkie** from the HAT desktop. Logs go
to `~/.whisplay-daemon/daemon-app.log`; raise the level with
`WALKIE_LOG_LEVEL=DEBUG ./run.sh`.
</details>

---

## Using it

| Screen | 1 click | 2 clicks | 3 clicks | hold |
|---|---|---|---|---|
| **Contacts** | next station | open Talk | Status | talk to selection |
| **Talk** | Inbox | back to Contacts | replay last voice | **talk** |
| **Inbox** | next message | back to Talk | play it | talk |
| **Status** | Contacts | Contacts | Settings | talk |
| **Settings** | next setting | open it | back to Contacts | talk |
| *editor* | change value | next field / save | cancel | — |

Four clicks exits from anywhere. Hold-to-talk works on every screen —
you should never have to navigate somewhere before you can answer.

There are two kinds of screen. **Menus** — Contacts and Settings — are
lists you pick from, so two clicks opens the highlighted row and three
goes back. **Views** — Talk, Inbox and Status — are places you already
are, so two clicks leaves. Three clicks means play wherever there is
something to play. An empty inbox leaves on any click rather than
sitting there ignoring you.
Both the dispatcher and the on-screen hints come from one table in
[app/ui/navigation.py](app/ui/navigation.py), so a screen cannot
advertise a gesture the app does not implement — which is exactly how
the inbox once ended up printing "2 clicks back" while two clicks did
nothing at all.

The header carries signal strength and a duty-cycle bar that only draws
attention once the hour's budget is running low.

---

## Settings

Everything that identifies a node can be set on the device, with the
button — no editing files over SSH. **Status → 3 clicks → Settings.**

| Setting | What it does |
|---|---|
| **Device ID** | this node's radio address, 0–65534 |
| **Base station** | which contact counts as base |
| **Add device** | pair another radio by address |
| **Date & time** | fixes timestamps on a Pi with no RTC |
| **Reset all data** | erases messages, voice clips, roster and settings |

Editors are driven by the same click language: **1 click** changes the
value under the cursor, **2 clicks** moves to the next field and saves
on the last one, **3 clicks** cancels everything. Digits are edited
most-significant first, with the cursor underlined. Hold-to-talk is
suspended while an editor is open — a hold there would transmit a
half-typed address, and you are plainly not trying to talk.

Reset starts on **no**, and you have to click onto **YES** before
confirming, so no reflex gesture can wipe the inbox.

Changes are saved to `~/.whisplay-walkie/settings.json`, not back into
`config.yaml` — rewriting that would destroy the comments that explain
it, and it is version controlled and shared between nodes while a
device's address must be unique to it. Precedence is defaults <
`config.yaml` < device settings < environment, so a one-off
`WALKIE_RADIO_ADDRESS=9 ./run.sh` still wins for debugging.

> **Changing Device ID does not reprovision the radio.** The module
> filters incoming packets using the address in its own registers, and
> only `provision_radio.py` can change that — which needs the LCD's GPIO
> pins. After changing the ID, run
> `sudo ./tools/provision.sh --address <id>` so the module agrees. The
> app says so on screen and in the log.

**Every node needs a unique address.** Two nodes sharing one cannot talk:
each discards the other's traffic as its own echo. The app refuses a
contact that shares this node's address at startup, counts and explains
the dropped packets, and `deploy.sh` no longer copies `config.yaml`
between devices — which used to hand every node the same identity.

## Power

The Pi Zero 2 W is expected to run from a battery, so idle cost is a
design constraint rather than an afterthought. Measured on the device,
foregrounded and listening, over a 15-second idle window:

| | measured |
|---|---|
| CPU | **0.000 %** of one core (0 ticks) |
| Wakeups | **0** context switches |
| Memory | 39 MB RSS, 5 threads |
| CPU temperature | 37.6 °C |

Not "low" — *zero*. Every thread is parked in a blocking syscall, so a
quiet radio genuinely costs nothing until a byte, a button edge or a
backlight deadline arrives. How:

- **No polling anywhere.** The serial reader blocks in a kernel `read()`,
  the transmit thread blocks on an empty queue, the button worker blocks
  on a condition variable, and the main loop blocks on an event with no
  timeout when nothing is pending. (The stock Waveshare example spins on
  `ser.inWaiting()` and pins a core permanently.)
- **Frames are hashed before they are pushed.** An unchanged screen costs
  nothing; `frames_skipped` in the shutdown log shows how many redraws
  were avoided.
- **The backlight — the largest single draw — dims after 25 s and turns
  off after 120 s**, and comes straight back on any button press or
  received packet. Tune under `ui:` in `config.yaml`.
- **The audio codec is opened only while recording or playing**, never
  held open across an idle radio.
- **Presence beacons are off by default.** Each one spends duty-cycle
  budget; set `power.beacon_interval_seconds` if you want them.

---

## How it works

```
  button ──▶ GestureDetector ──▶ WalkieApp ──▶ ViewState ──▶ screens ──▶ framebuffer
                                    │
   mic ──▶ arecord ──▶ Codec2 ──────┤
                                    ▼
                                 LoraLink ──▶ protocol ──▶ framing ──▶ SX126x ──▶ UART
                                    │         fragment      CRC+len
                                    ▼
  speaker ◀── aplay ◀── Codec2 ◀── Reassembler ◀── Deframer ◀── blocking read
```

| Module | Responsibility |
|---|---|
| [app/radio/sx126x.py](app/radio/sx126x.py) | UART driver; blocking reads, optional GPIO, persistent config |
| [app/radio/framing.py](app/radio/framing.py) | Length-prefixed CRC frames over a byte stream |
| [app/radio/protocol.py](app/radio/protocol.py) | Packet header, fragmentation, reassembly |
| [app/radio/airtime.py](app/radio/airtime.py) | Duty-cycle budget |
| [app/radio/link.py](app/radio/link.py) | Threads, queueing, pacing, peer tracking |
| [app/audio/codec2.py](app/audio/codec2.py) | `ctypes` binding to libcodec2 |
| [app/ui/screens.py](app/ui/screens.py) | Pure render functions of `ViewState` |
| [app/main.py](app/main.py) | State machine and the event-driven main loop |

### On-air format

```
[ dst_hi dst_lo chan ]   consumed by the module (fixed-point addressing)
[ AA 55 | len | header(7) | body | crc16 ]   ← what goes on the air
         header: ver+type | src(2) | msg_id | seq | total | flags
```

The module strips the first three bytes before transmitting and appends
one RSSI byte to everything it receives. Framing is length-prefixed
rather than delimiter-based precisely because of that trailing byte: a
delimiter parser cannot tell it from the start of the next frame without
holding each message back until another one arrives, which on a quiet
channel is indistinguishable from a dead radio.

Voice fragments are never retransmitted — at 700 bps a retry costs more
airtime than the gap it fills — so a missing fragment becomes silence of
the right duration and the message plays anyway, marked *(gaps)*.

---

## Testing

```bash
python3 -m pytest tests -q     # 54 tests, no hardware required
```

Measured on the Pi with the HAT attached, the full audio path runs well
inside real-time — a 20-second clip encodes in under a second:

```
captured   48000 B PCM = 3.0 s
encoded      300 B in 118 ms   (25.3x realtime)
fragmented     2 packets, 324 B on the wire
decoded    48000 B PCM in 36 ms
```

`tests/fakes.py` models the module rather than just a serial port: it
consumes the three address bytes on transmit and appends an RSSI byte on
receive, so `tests/test_link_end_to_end.py` runs two complete radios
against each other — text, multi-fragment voice, a dropped fragment, and
a duty-cycle exhaustion — entirely in software.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No such file or directory: '/dev/ttyS0'` | UART disabled | Step 2, then reboot |
| Garbled or one-way traffic | Login console on the port | Step 3 |
| `radio offline` on screen | Port busy or HAT unseated | `fuser -v /dev/ttyS0` |
| Nothing received | Address or frequency mismatch | `provision_radio.py --check` on both |
| `no microphone` / `no audio hardware` | Sound card not registered | See below |
| Screen black after exiting the app | Fixed — see below | Update; the app now hands the backlight back lit |
| Screen black after returning to the app | Fixed — see below | Update; the app re-attaches on the daemon's grant |
| Display stays blank at launch | Another app holds the foreground | The app retries every 5 s; check `app.list` |
| Sending stalls, "duty cycle full" | Hour's airtime spent | Wait, or raise `duty_cycle_percent` where licensed |

**Black screen (fixed in this app, worth knowing about).** Two separate
daemon behaviours both end in a dark panel, and both bit this app:

1. **The daemon sets the backlight exactly once, at its own startup.**
   `_release_focus` re-renders the desktop but never touches brightness,
   so the desktop simply inherits whatever the last foreground app left.
   An app that blanks the screen on exit hands back a desktop being
   drawn perfectly onto an unlit panel. The app now calls
   `Display.restore_backlight()` on the way out — never `set_backlight(0)`.

2. **`whisplay_client` ignores `app_foreground_acquired`.** When you
   return to an app that is still running, the daemon does not wait to be
   asked: it calls `_grant_focus` itself, which mints a new session token
   and *reallocates the framebuffer*, then broadcasts that event. The
   stock client handles the other four events and drops this one, so the
   app keeps drawing into a torn-down mapping while the daemon, believing
   the app is foreground, stops drawing the desktop. Nobody paints
   anything. `board.watch_foreground_grants()` listens for that event and
   re-attaches using the token from its payload.

   Two traps live there: never *poll* `acquire_foreground` (after a back
   gesture the desktop is showing, and polling snatches the screen from
   the user every few seconds), and never call `acquire_foreground` from
   the handler — it makes the daemon grant focus, which rebroadcasts the
   event, which re-enters the handler. That loop was measured at 24 focus
   grants per second.

If you ever see the screen thrashing and a core pinned, check for two
copies of the app: `pgrep -f "app.main"`. Two instances ping-pong the
foreground between themselves and interleave bytes into the same radio.
There is now an `flock` guard that refuses the second one.

**Sound card not registering.** Voice needs a working capture device.
Check whether the card ever finished probing:

```bash
cat /proc/asound/cards                        # only vc4hdmi = not registered
arecord -l                                    # no capture devices
sudo cat /sys/kernel/debug/devices_deferred   # "sound" = stuck in deferred probe
```

If the Whisplay codec is stuck waiting for its regulators
(`/sys/bus/i2c/devices/1-0010/waiting_for_supplier`), that is a driver
or overlay problem in the HAT's out-of-tree module, not in this app.
**Text messaging and everything else keep working without it** — the
voice path reports itself unavailable and the app runs on.

---

## Licence and credits

Radio register layout derived from Waveshare's SX126X HAT sample code.
Speech coding by [Codec2](https://www.rowetel.com/codec2.html) (David
Rowe, LGPL). Display and button access through the Whisplay daemon.
