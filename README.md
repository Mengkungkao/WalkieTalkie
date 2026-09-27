# WalkieTalkie

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
mean the module can never be reprovisioned without unsoldering. Device
IDs are not affected — they live in the app, not the module — but if you
expect to change frequency or air rate, take option A.

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

`setup.sh` works out whether it is on a Raspberry Pi or an Orange Pi
Zero 2W and runs [setup/raspberrypi.sh](setup/raspberrypi.sh) or
[setup/orangepi.sh](setup/orangepi.sh); `--board orangepi` overrides the
guess. Either one walks all eight steps below, asks before it changes
anything, and is safe to re-run. `--check` reports without changing;
`--yes` runs unattended. The Orange Pi differs in a few places — see
[Orange Pi Zero 2W](#orange-pi-zero-2w).

From a development machine, one command copies the project over,
installs it, and adds **WalkieTalkie** to the Whisplay HAT's desktop. It
opens an ssh session for setup's questions and the device's sudo
password:

```bash
./deploy.sh jarvis@192.168.0.33 --setup
```

Nothing else needs configuring by hand. A fresh install picks its own
Device ID and uses the hostname as its name; to talk to another radio,
pair with it from the app — see [Pairing](#pairing).

### The long way, step by step

<details open>
<summary><b>Step 1 — System packages</b></summary>

```bash
sudo apt update
sudo apt install -y python3-serial python3-yaml python3-pil python3-numpy \
                    python3-cryptography libcodec2-1.2 alsa-utils python3-pytest
```

`libcodec2` is a **system shared library**, loaded with `ctypes` — there
is no Python wheel to build and no `-dev` package needed. Verify:

```bash
python3 -c "import ctypes.util; print(ctypes.util.find_library('codec2'))"
# libcodec2.so.1.2
```

Debian 12 and Ubuntu 22.04 package it as `libcodec2-1.0` instead; that
works too, and `setup.sh` picks whichever the release has.
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
python3 provision_radio.py --frequency 868
sudo systemctl start whisplay-daemon
```

Every module gets the **same** settings — the same `--frequency` and air
rate. The address the module stores no longer matters: the app puts the
destination in its own packet header, so a provisioned LoRa HAT works in
any radio. Read back what a module currently holds without changing it:

```bash
python3 provision_radio.py --check
```
</details>

<details open>
<summary><b>Step 6 — Configure the app</b></summary>

Usually nothing to do. The shipped [config.yaml](config.yaml) leaves
identity to the device, and contacts come from pairing:

```yaml
identity:
  callsign: auto         # this machine's hostname
radio:
  address: auto          # an unused Device ID, picked on first start and kept
  frequency_mhz: 868
  duty_cycle_percent: 1.0  # ETSI EU868. Raise only where licensed.
contacts: []             # pair on the device: Home > Pair devices
```

Set `callsign` to name a radio something other than its hostname.
Stations you have not paired with still appear on the contacts screen
once they transmit.
</details>

<details open>
<summary><b>Step 7 — Register with the Whisplay daemon</b></summary>

```bash
./install.sh                # adds "WalkieTalkie" to the HAT desktop
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

…or long-press to launch **WalkieTalkie** from the HAT desktop. Logs go
to `~/.whisplay-daemon/daemon-app.log`; raise the level with
`WALKIE_LOG_LEVEL=DEBUG ./run.sh`.
</details>

### Orange Pi Zero 2W

Both HATs fit its 40-pin header. Set up from a development machine the
same way as a Pi:

```bash
./deploy.sh orangepi@192.168.0.130 --setup   # asks for sudo; reboot when told
ssh orangepi@192.168.0.130 'cd WalkieTalkie && ./setup.sh --check'   # after it
```

[setup/orangepi.sh](setup/orangepi.sh) handles what is different from the Pi:

- **The LoRa port is the debug console.** Header pins 8/10 are UART0
  (`/dev/ttyS0`). Orange Pi OS ships with `console=both`, and logs a
  shell in on that port automatically: whatever the radio receives is
  typed into it, and the shell's output is transmitted. The console is
  set in `/boot/orangepiEnv.txt` (`armbianEnv.txt` on Armbian), not
  `cmdline.txt`; setup changes it to `console=display`, which also stops
  the auto-login after a reboot. U-Boot still prints to the port for a
  moment at power-on.
- **The Whisplay HAT needs PiSugar's Orange Pi driver.** It enables SPI1
  for the LCD and adds the `whisplaysound` card, which the app picks
  automatically. Without it the app runs headless, with no screen or
  button:

  ```bash
  git clone --depth 1 https://github.com/PiSugar/Whisplay.git ~/Whisplay
  cd ~/Whisplay && sudo bash script/install_orangepi_zero2w.sh
  sudo reboot
  ```

- **The M0/M1 clash is the same.** The jumpers land on header pins 15
  and 13, which here are PI5 and PH3, the Whisplay backlight and DC
  lines. Remove the jumpers exactly as on the Pi.
- **The radio cannot be provisioned from the Orange Pi yet.**
  `provision_radio.py` and `radio.mode_pins` drive M0/M1 through
  `RPi.GPIO`, which only runs on a Raspberry Pi. The settings are stored
  in the module, and every module gets the same ones, so provision the
  LoRa HAT once on any Pi, then move it across. Device ID and pairing are
  in the app and work the same on both boards.

---

## Using it

The app opens on a menu:

```
WALKIE                         orangepizero2w · ID 6235 · ch 3
  Start           ──▶  To ALL               ──▶  Talk to every paired radio
                       To a paired device   ──▶  pick one  ──▶  Talk
  Receive         ──▶  what has come in, newest first
  Pair devices    ──▶  find another radio and pair with it
  Settings        ──▶  name, Device ID, privacy channel, …
```

| Screen | 1 click | 2 clicks | 3 clicks | hold |
|---|---|---|---|---|
| **Home** | next row | open it | Status | talk |
| **Start** | next row | open it | back | talk |
| **Paired** | next radio | talk to it | back | talk |
| **Talk** | Receive | back | replay last voice | **talk** |
| **Receive** | next message | back | play it | talk |
| **Status** | back | back | Settings | talk |
| **Settings** | next setting | open it | back | talk |
| **Pair** | next radio found | pair with it | back | talk |
| *editor* | change value | next field / save | cancel | — |

Four clicks exits from anywhere. Hold-to-talk works on every screen and
talks to whoever you last chose under Start — ALL until you pick someone
— so you never have to navigate somewhere before you can answer. Home
shows who that is (`now talking to jarvis`).

There are two kinds of screen. **Menus** — Home, Start, Paired, Settings
and Pair — are lists you pick from, so two clicks opens the highlighted
row and three goes back. **Views** — Talk, Receive and Status — are
places you already are, so two clicks leaves. "Back" returns to wherever
you came from: Receive opened from Talk goes back to Talk, opened from
Home goes back Home. Three clicks means play wherever there is something
to play. An empty Receive screen leaves on any click rather than sitting
there ignoring you.

Both the dispatcher and the on-screen hints come from one table in
[app/ui/navigation.py](app/ui/navigation.py), so a screen cannot
advertise a gesture the app does not implement — which is exactly how
the inbox once ended up printing "2 clicks back" while two clicks did
nothing at all.

The header carries signal strength and a duty-cycle bar that only draws
attention once the hour's budget is running low.

---

## Telling the radios apart by ear

Two identical Pis on one desk raise the same question every time
something beeps: was that mine or theirs? Each station has its own
pitch, drawn from a pentatonic ladder so any two are clearly different
and no pair beats against the other.

| | sounds |
|---|---|
| your transmission starting | one note, **your** pitch |
| your transmission sent | rising pair, your pitch |
| an incoming message | falling pair, the **sender's** pitch |
| an error | a low buzz, the same for everyone |

Pattern carries the meaning and pitch carries the identity, so "rising
means I am sending" is learned once and then tells you *which* radio did
it. An incoming call announces who is calling before a word is decoded.

The pitch comes from the node address, so changing Device ID changes the
sound. With more than eight stations two will eventually share a pitch;
the app checks its contacts at startup and says so rather than letting
the feature quietly stop working.

## Connecting two radios

Two radios talk once they have **paired**. Pairing exchanges keys, so
everything after it is encrypted — see [Privacy and
security](#privacy-and-security). There is no other way to add a radio:
typing in an ID cannot set up keys.

### Pairing

On **both** radios: **Home → Pair devices**.

```
this radio: orangepizero2w · ID 6235
    code 4821 · waiting for jarvis

 ┌──────────────────────────────┐
 │ jarvis                       │   ← 1 click next, 2 clicks pair
 │ ID 1234  ·  strong           │
 └──────────────────────────────┘
```

1. Each radio announces itself every 3 seconds while the screen is open,
   and lists the other radios it hears doing the same. Both must be on
   the same privacy channel.
2. On one radio, highlight the other and **2 clicks** to pair. It shows a
   four-digit **code** and waits.
3. The other radio asks: *orangepizero2w wants to pair — code 4821*.
   **Check the code is the same on both screens.** Then click onto
   **YES** (it starts on **no**) and 2 clicks.
4. Both radios save each other, with keys, and are connected. Each lands
   on the Paired list with the new radio selected.

The code is what makes pairing safe over the air. It is computed from
both radios' keys, so anyone who slipped their own key into the exchange
would make the two screens show different codes. If they differ, say no.

A refusal says so (`jarvis said no`), and nothing is saved on either
side: the radio that asked saves the other only once the answer arrives,
and only if the answer carries the same key its beacon did. The window
closes by itself after two minutes, and leaving the screen — 3 clicks,
or holding to talk — stops it. A radio that is not pairing ignores
beacons and requests, so a stranger nearby can neither show up on your
lists nor make your radio ask you anything. Two minutes of pairing costs
a few seconds of the hour's duty-cycle budget.

**Two radios with the same ID.** Fresh installs pick a random ID, so this
is rare, and pairing catches it when it happens: each beacon carries a
random token the radio chose at install time. A radio that hears its own
ID with someone else's token knows it has a twin. If it is the one
pairing, it picks a new ID and says so (`ID 5 was taken: now 40213`); if
not, it answers once so the pairing radio learns of the clash and moves.

### Staying connected

Hearing a station does not prove it hears you, and a one-way link is the
classic radio failure — you talk for a minute before discovering nobody
received a word. So a station counts as connected only once it has
**answered**:

```
MengPi  ──hello──▶  jarvis      jarvis knows MengPi is on the air
MengPi  ◀─hello-ack─  jarvis    both ends now know the link carries
```

It happens on its own. At startup each radio calls every radio it has
paired with, and opening **Talk** on one that is not connected calls it
again. The hello is sealed with the pair's key, so it also proves the
other radio still holds it, and it carries the other radio's current
name, which updates the Paired list.

The Paired list shows the state as a dot: **filled green** answered,
**hollow amber** calling, **red** refused, **amber** heard but not
handshaked, **grey** never heard. A contact from before pairing existed
says `not paired: pair again`. Talk says `not connected` under the disc
before you transmit, and Status has a `peer` row.

## Privacy and security

**Privacy channels.** Settings → Privacy channel picks one of 16. Radios
on different channels share the frequency but ignore each other
completely — like the privacy codes on a handheld walkie-talkie. It is a
filter, not secrecy: that is what encryption is for.

**Encryption.** Every radio makes its own key pair on first start. When
two radios pair they agree a key only the two of them hold, and each
hands the other its *broadcast key*, sealed so nobody listening can read
it. Then:

| You talk to | Sealed with | Who can listen |
|---|---|---|
| one paired radio | the pair's own key | that radio |
| ALL | your broadcast key | every radio you have paired with |

So **ALL means everyone you have paired with**, not everyone on the air.
Anything unsealed, sealed with a key this radio does not hold, altered in
flight, or recorded and played back is dropped before the app sees it.
Only the pairing messages themselves travel in the clear, and even those
carry the broadcast key sealed.

What it does not hide: that radios are transmitting, their Device IDs,
names and channel (the header is in the clear so receivers can route on
it, though it is authenticated), and message sizes and timing.

**Keys** live in `~/.whisplay-walkie/keys.json`, readable only by the
owner, and never leave the device — `deploy.sh` does not touch that
directory. **Reset all data** makes new keys, so every radio has to be
paired again. The cost of all this is 22 bytes per packet, about an
eighth of each one.

## Settings

Everything that identifies a radio can be set on the device, with the
button — no editing files over SSH. **Home → Settings.**

| Setting | What it does |
|---|---|
| **Name** | what other radios see: the hostname, or Alpha … Zulu |
| **Device ID** | this radio's ID, 0–65534; takes effect at once |
| **Privacy channel** | 1–16; only radios on the same channel hear you |
| **Base station** | which paired radio counts as base |
| **Date & time** | fixes timestamps on a Pi with no RTC |
| **Reset all data** | erases messages, voice clips, paired radios, keys and settings |

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
it, and it is version controlled and shared between radios while a
radio's ID must be unique to it. Precedence is defaults < `config.yaml`
< device settings < environment, so a one-off
`WALKIE_RADIO_ADDRESS=9 ./run.sh` still wins for debugging.

**Device ID takes effect immediately.** It used to need the module
reprovisioning, because the module dropped packets not addressed to the
number in its own registers — and rewriting those needs the mode pins,
which the LCD owns on a Pi and which an Orange Pi cannot drive at all.
Now every packet is a module broadcast carrying its destination in the
header, and the app filters on that itself. Radios that paired with the
old ID still have it saved, so pair with them again; the app reminds you
(`ID 9 · re-pair others`).

**Every radio needs a unique ID.** Two radios sharing one cannot talk:
each discards the other's traffic as its own echo. Fresh installs pick a
random one, pairing detects and fixes clashes, and `deploy.sh` never
copies `config.yaml` between devices.

**Upgrading from an older version.** The packet format changed (protocol
v3: privacy channels and encryption), so update every radio, then pair
them. Radios on different versions cannot hear each other at all, and
contacts saved before this version have no keys: they show `not paired`
until paired again.

## The backlight is also the radio's M0

With the LoRa HAT's stock M0/M1 jumpers fitted, the two boards share two
lines. Whisplay's source numbers pins in BOARD mode, which is why this is
easy to miss:

| Whisplay | BOARD | BCM | Function | LoRa |
|---|---|---|---|---|
| `LED_PIN` | 15 | **22** | LCD backlight | **M0** |
| `DC_PIN` | 13 | **27** | LCD data/command | **M1** |

The backlight is **active-low and dimmed by 1 kHz PWM**
(`duty_cycle = 100 - brightness`), so:

| Backlight | BCM 22 | M0 | Radio |
|---|---|---|---|
| 100% | steady low | 0 | **transparent — works** |
| 0% | steady high | 1 | wrong mode — deaf |
| anything between | PWM at 1 kHz | toggling | mode thrashing |

So on this stack **hearing costs the screen**: the app pins brightness at
100% and stands the idle policy down, saying so on the Status screen. It
detects the clash from `radio.mode_pins`, so rewiring the mode pins to
free GPIOs brings the dimming — and the power saving — straight back.

`DC_PIN` is the other half, and only Whisplay can fix it: `_send_data`
and `_send_data_bytes` raise DC and never lower it, so after any frame
flush BCM 27 rests high and the module sits in configuration mode. Ending
each data transfer with DC low costs one GPIO write and is invisible to
the display, which only samples DC while SPI is clocking.

The change is written up as [docs/whisplay-dc-fix.patch](docs/whisplay-dc-fix.patch),
to apply in the Whisplay checkout:

```bash
cd ~/Whisplay && git apply ~/WalkieTalkie/docs/whisplay-dc-fix.patch
sudo systemctl restart whisplay-daemon
```

Once it lands, the Status screen's `mode pins` row should read
`transparent` instead of `configuration`, and the two radios link on
their own.

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
[ FF FF chan ]   consumed by the module: always a module broadcast
[ AA 55 | len | header(10) | body | crc16 ]   ← what goes on the air
         header: ver+type | channel+sealing | src(2) | dst(2) | msg_id | seq | total | flags
         body:   salt(6) | ciphertext | tag(16)      ← sealed, ChaCha20-Poly1305
```

Every radio on the frequency hears every packet. It drops those on
another privacy channel, keeps those whose `dst` is its own Device ID or
broadcast, and opens the body with the key the sealing field names — the
pair's key or the sender's broadcast key — using the header as
associated data, so no field of it can be changed without the packet
failing to open. Each fragment is sealed on its own, so a lost fragment
never makes the rest of a voice message unreadable. See
[app/radio/crypto.py](app/radio/crypto.py).

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

## Measuring the link

Before trusting voice, check that bytes cross at all and how fast:

```bash
# Pi Zero
python3 tools/throughput.py --echo

# Pi 4B
python3 tools/throughput.py --ramp --to 51
```

It starts with `Hello world` and works upward, so a failure on the first
rung is unmistakably the link rather than the payload. Every payload is
echoed back, because a one-way test passes on a radio that can hear but
cannot be heard — which is the failure this hardware actually had.

```
   bytes frags      rtt    thruput     rssi  result
      11     1    0.42s      26 B/s  -74 dBm  ok
     193     2    1.12s     172 B/s  -76 dBm  ok
     386     3    1.83s     211 B/s  -75 dBm  ok
```

Throughput is payload bytes over the **round trip**, so it counts both
directions plus the far end's turnaround — roughly half the one-way rate,
and the number that matters when you are waiting for an answer. Airtime
used is reported against the hour's 1% duty-cycle budget.

The mode pins are checked before anything is sent. A module in
configuration or sleep mode accepts every byte over the UART and radiates
none of them, so without that check a wiring fault is indistinguishable
from a range problem:

```
! the module is in configuration mode (M0=GPIO5 M1=GPIO6 read (0, 1)).
  It will accept every byte over the UART and radiate none of them.
```

The app must be stopped first, since it holds the port:
`systemctl --user stop walkie-talkie.service`

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
| `ModuleNotFoundError: No module named 'serial'` | System packages not installed | `./setup.sh` (step 2), or Step 1 by hand |
| `No such file or directory: '/dev/ttyS0'` | UART disabled | Step 2, then reboot |
| Garbled or one-way traffic | Login console on the port | Step 3; on an Orange Pi, `./setup.sh` and reboot |
| `fuser -v /dev/ttyS0` shows `bash` (Orange Pi) | Auto-login shell on the console | `./setup.sh` and reboot |
| `cannot drive M0/M1 on this board` | Provisioning from an Orange Pi | Provision the HAT on a Pi |
| `radio offline` on screen | Port busy or HAT unseated | `fuser -v /dev/ttyS0` |
| Nothing received | Frequency or air-rate mismatch | `provision_radio.py --check` on both |
| Two radios cannot hear each other at all | Different privacy channels, or versions | Same channel in Settings; update both |
| `pair with X first` | A contact from before encryption | Pair again: Home → Pair devices |
| Pairing lists nobody | The other radio is not on its Pair screen, or on another channel | Open Pair devices on both, same channel |
| A paired radio stopped answering after an ID change | It still has the old ID | Pair again |
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
