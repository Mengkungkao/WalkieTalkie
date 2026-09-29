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
fragment → transmit → reassemble → decode → play. The app tracks the
airtime budget in a rolling one-hour window and refuses to transmit past
it ([app/radio/airtime.py](app/radio/airtime.py)).

### Voice quality

700C is intelligible but sounds robotic, so the default is now **3200**,
Codec2's clearest mode, and **Settings → Voice quality** trades clarity
for airtime. Each message says which mode it is in, so radios set
differently still understand each other.

| Voice quality | STOI | 10 s message | fragments | airtime | per hour @ 1% |
|---|---|---|---|---|---|
| **Clear (3200)** — default | **0.87** | 4.0 kB | 24 | 7.0 s | 5 |
| Balanced (1600) | 0.83 | 2.0 kB | 12 | 3.5 s | 10 |
| Most messages (700C) | 0.73 | 1.0 kB | 6 | 1.7 s | 20 |

STOI is an objective intelligibility score (0–1; 1 is the original),
measured on recorded human speech through the app's whole voice path;
airtime is sealed and framed, at 9600 bps. A message is on the air — and
reaches the other radio — about that long after you let go, and short
messages cost proportionally less: three seconds at Clear is about two
seconds of air. If "duty cycle full" starts turning messages away, step
down to Balanced, which loses little clarity for half the airtime.

The voice path matters as much as the codec. The sound cards run at
48 kHz — the Orange Pi's offers nothing else — and ALSA's own conversion
to and from Codec2's 8 kHz does not filter: it folded hiss into the
voice on the way in (speech scored 0.957 before any codec touched it)
and left a metallic edge on the way out. The app now opens the cards at
48 kHz and converts itself, with a proper low-pass filter
([app/audio/dsp.py](app/audio/dsp.py)), then high-passes speech at
120 Hz and levels it to −9 dBFS before encoding. With the old path, 3200
scored only 0.75; the filtering is what lets the better codec show.

**The microphone level** is set by the app at every start
(`audio.mic_level`, 80 by default). The Whisplay driver's `mic` control
is analog boost as much as volume — 80% is +20 dB, 100% is +29 dB — and
at 100% ordinary speech overdrives the preamp. That is distortion before
anything digital happens, so no filter or codec can take it out again:
the Pi, left at 100%, sounded muddy to the Orange Pi while the Orange Pi,
at 80%, sounded clear to the Pi. Each recording now logs its peak and
how much of it clipped, and the screen says `mic too loud` when it did.

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

Each board has its own installer. On the device:

| Board | Installer |
|---|---|
| Raspberry Pi (Zero 2 W, 4B), Raspberry Pi OS | `./install-raspberrypi.sh` |
| Orange Pi Zero 2W, Orange Pi OS or Armbian | `./install-orangepi-zero2w.sh` |

```bash
git clone git@github.com:Mengkungkao/WalkieTalkie.git
cd WalkieTalkie
./install-raspberrypi.sh          # or ./install-orangepi-zero2w.sh
```

> This repository is private, so a plain HTTPS clone onto a headless Pi
> will stop at `could not read Username for 'https://github.com'`.
> [docs/cloning-to-a-pi.md](docs/cloning-to-a-pi.md) covers the three
> ways through that — agent forwarding, a deploy key, or an account key
> — and the errors each one produces when it is the wrong choice.

Each installs the packages, frees the LoRa serial port, checks the
pins, audio and Whisplay driver, registers **WalkieTalkie** on the HAT's
desktop, provisions the radio where the board can, and runs the tests.
It asks before it changes anything and is safe to re-run; `--check`
reports without changing, `--yes` runs unattended. They share their
helpers ([setup/common.sh](setup/common.sh)) and differ where the boards
do — see [Orange Pi Zero 2W](#orange-pi-zero-2w). `./setup.sh` works out
which board it is on and runs the right one, which is what `deploy.sh`
uses.

From a development machine, one command copies the project over,
installs it, and adds **WalkieTalkie** to the Whisplay HAT's desktop. It
opens an ssh session for setup's questions and the device's sudo
password:

```bash
./deploy.sh jarvis@192.168.0.33 --setup      # a Raspberry Pi
./deploy.sh orangepi@192.168.0.130 --setup   # an Orange Pi Zero 2W
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

Writing the module's settings needs M0/M1 for a few seconds. The tool
stops the display daemon (which holds them), writes the settings, and
starts it again, so it needs sudo. Settings are stored in non-volatile
memory and survive power cycles, so this is done once, not at every
start. It works the same on a Raspberry Pi and an Orange Pi Zero 2W.

```bash
sudo python3 provision_radio.py --frequency 868
```

Every module gets the **same** settings — the same `--frequency` and air
rate. The address the module stores no longer matters: the app puts the
destination in its own packet header, so a provisioned LoRa HAT works in
any radio. Read back what a module currently holds without changing it:

```bash
sudo python3 provision_radio.py --check
```

For more range, see [Range](#range): `--range long`.
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

Both HATs fit its 40-pin header. Install from a development machine:

```bash
./deploy.sh orangepi@192.168.0.130 --setup   # asks for sudo; reboot when told
ssh orangepi@192.168.0.130 'cd WalkieTalkie && ./install-orangepi-zero2w.sh --check'
```

[install-orangepi-zero2w.sh](install-orangepi-zero2w.sh) handles what is
different from the Pi:

- **The LoRa port is the debug console.** Header pins 8/10 are UART0
  (`/dev/ttyS0`). Orange Pi OS ships with `console=both` and a systemd
  drop-in that logs `orangepi` in on that port automatically. The shell
  reads whatever the radio receives as typing, its output is
  transmitted, it takes bytes the app was waiting for — so fragments
  arrive short and voice breaks up — and every time it restarts it hangs
  the port up, which is what `write failed: [Errno 5] Input/output
  error` in the log means.

  The console is set in `/boot/orangepiEnv.txt` (`armbianEnv.txt` on
  Armbian), not `cmdline.txt`, and **`console=display` is not enough on
  Orange Pi OS**: its boot script adds `console=ttyS0` for `display` as
  well as `both`. The installer reads `/boot/boot.cmd` to tell, and on
  that image sets `console=none` with `extraargs=console=tty1`. It also
  masks `serial-getty@ttyS0` outright. After the reboot, check:

  ```bash
  tr ' ' '\n' < /proc/cmdline | grep console   # console=tty1 only
  fuser -v /dev/ttyS0                          # nothing
  ```

  U-Boot still prints to the port for a moment at power-on.
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
- **So is the DC fix, and PiSugar's Orange Pi installer does not have
  it.** Without it the module sits in configuration mode — it sends
  nothing and hears nothing — which is why the first Orange Pi could not
  find the Pi to pair. Both installers check for it and apply
  [docs/whisplay-dc-fix.patch](docs/whisplay-dc-fix.patch); see
  [The backlight is also the radio's M0](#the-backlight-is-also-the-radios-m0).
- **Provisioning works here too.** `provision_radio.py` drives M0/M1
  through libgpiod — lines 261 and 227 of the H618's pin controller here,
  22 and 27 on a Pi — so the module can be set on either board. Device ID
  and pairing are in the app and work the same on both boards.

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
  Range test      ──▶  probe a paired radio and log the signal (for testing)
```

| Screen | 1 click | 2 clicks | 3 clicks | hold |
|---|---|---|---|---|
| **Home** | next row | open it | Status | — |
| **Start** | next row | open it | back | talk |
| **Paired** | next radio | talk to it | back | talk |
| **Talk** | Receive | back | replay last voice | **talk** |
| **Receive** | next message | back | play it (and fetch any gaps) | — (listen only) |
| **Status** | back | back | Settings | — |
| **Settings** | next setting | open it | back | — |
| **Pair** | next radio found | pair with it | back | — |
| **Range test** | mark this spot | stop | probe now | talk to the radio under test |
| *editor* | change value | next field / save | cancel | — |

Four clicks exits from anywhere. **Holding the button talks only inside
Start** — on the Start menu, the Paired list and Talk — to whoever you
last chose there: ALL until you pick someone. (And on the Range test, to
the radio under test, so voice can be tried at each spot.) Home shows who that is
(`now talking to jarvis`). Everywhere else a hold does nothing and says
so: menus are for choosing, and a hold that transmitted while you were
looking for a setting went out to whoever was last chosen, unasked.
**Receive is for listening** — to what has arrived, and to new messages,
which still play as they come in — not for talking. The microphone is
only kept warm where a hold can talk, which also saves power.

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
   and lists the other radios it hears doing the same — on any privacy
   channel; a radio on another channel shows its channel (`ch 2`).
2. On one radio, highlight the other and **2 clicks** to pair. It shows a
   four-digit **code** and waits.
3. The other radio asks: *orangepizero2w wants to pair — code 4821*.
   **Check the code is the same on both screens.** Then click onto
   **YES** (it starts on **no**) and 2 clicks.
4. Both radios save each other, with keys, and are connected. Each lands
   on the Paired list with the new radio selected. If they were on
   different channels, the one that asked moves to the channel of the one
   that said yes (`paired with jarvis · now on channel 1`), so they can
   hear each other afterwards.

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

### In range, or not

A handshake proves the link once. Whether the other radio is *still* in
range is checked all the time: every paired radio sends one small
sealed **ping** to all the radios it paired with every two minutes
(`radio.link_check_seconds`). Each ping also says how strongly its
sender last heard every other radio, so both ends learn both directions
of the link from one packet each:

```
jarvis   55 · in range · -84/-91 dBm       how I hear it / how it hears me
hilltop  77 · weak signal · -108/-112 dBm
rover    40 · disconnected · 6m ago
```

| State | Meaning |
|---|---|
| **in range** (green) | heard within the last two check intervals |
| **weak signal** (amber) | heard, but below −105 dBm one way or the other: one wall or hill from failing |
| **disconnected** (red ring) | not heard for two intervals — out of range, switched off, or its app closed |
| **keys changed** (red) | its packets arrive but cannot be opened: it was reset, so pair again |
| not checked yet (grey) | just started |

The paired list, Talk and Home (`1 paired · jarvis in range`) show it. A
radio dropping out says so with a banner and the error buzz; coming back,
with `jarvis back in range` and a chirp. Opening **Talk** on a radio not
heard in the last minute sends it a **probe** — a ping that asks for an
answer — so Talk says within a second or two whether you will be heard.

Cost: about 0.16 s of airtime per ping at 9.6k — 30 an hour is about
5 s, an eighth of the 36 s a 1% duty cycle allows. Checks stop by
themselves while less than a quarter of the hour's airtime is left, so
they never crowd out voice; set `link_check_seconds` higher to spend
less. Pings do not light the screen.

## Privacy and security

**Privacy channels.** Settings → Privacy channel picks one of 16. Radios
on different channels share the frequency but ignore each other —
like the privacy codes on a handheld walkie-talkie. The one exception is
pairing, which is heard on every channel while a radio's Pair screen is
open, so two radios set differently can still find each other. It is a
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
| **Voice quality** | Clear (3200), Balanced (1600) or Most messages (700C); see [Voice quality](#voice-quality) |
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
flush the line (BCM 27 on a Pi, PH3 on an Orange Pi) rests high and the
module sits in configuration mode — it answers every write with
`FF FF FF`, transmits nothing and hears nothing. Ending each data
transfer with DC low costs one GPIO write and is invisible to the
display, which only samples DC while SPI is clocking.

The change is [docs/whisplay-dc-fix.patch](docs/whisplay-dc-fix.patch).
Both installers check the Whisplay driver for it and apply it (then
restart the daemon); by hand:

```bash
cd ~/Whisplay && git apply ~/WalkieTalkie/docs/whisplay-dc-fix.patch
sudo systemctl restart whisplay-daemon
```

The app notices a module stuck like this by those `FF FF FF` replies and
says `radio in setup mode: run the installer`. On a Pi the Status
screen's `mode` row also reads `configuration` instead of `transparent`.

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

### When fragments go missing

At the edge of range a message rarely vanishes whole: a fragment or two
of it does. Three things now keep that from ruining it.

1. **The gap keeps its place.** Voice fragments carry exactly 168 bytes
   of speech — a whole number of Codec2 frames in every mode, and exactly
   what a sealed fragment holds. A lost one is kept as 168 bytes of
   space, which the app fills with encoded silence. Before, what did
   arrive was joined end to end, every later frame was out of step, and
   the rest of the message decoded as noise: the "crackle, can't
   understand it" failure.
2. **The receiver asks for what is missing.** Two seconds after the last
   fragment it expects — allowing for the ones still on their way — it
   sends the sender a small sealed `repair` request naming them. The
   sender keeps each message for a minute and resends just those, at most
   twice each; two listeners asking for the same broadcast fragment get
   one resend between them. Up to two rounds, then:
3. **What arrived is delivered anyway,** with the gaps silenced and the
   message marked *(gaps)*. A lost *last* fragment used to leave a
   message waiting until something else woke the app; the link now has
   its own timer for that, which sleeps when nothing is half-received.

4. **Or asked for again later.** Every radio now keeps what it *sends*
   too (with the rest of Receive, the newest 50 messages), so a message
   that arrived with gaps can be completed afterwards. **Replay it** —
   Receive, highlight it, three clicks: it plays as it is, and the radio
   asks the sender for just the missing parts. When they come the message
   is whole (`voice from jarvis: complete now`) and plays again. Its row
   says `(gaps · play to fix)` beforehand and `(fetching…)` while asking.
5. **A message missed outright is fetched once back in range.** Pings
   list the sender's last three voice messages from the past half hour;
   a radio that finds one it does not have asks for it whole and plays
   it (`missed voice from jarvis`). One that has gaps is asked for again
   the same way. Each is tried at most twice.

The request names the message by its number and a fingerprint of one
fragment the asking radio does hold, because message numbers are one
byte and come round again; the sender answers only if its copy matches,
and only a radio the message was sent to. Repairs and fetches cost
airtime only when something was lost, and count against the duty-cycle
budget like anything else.

---

## Range

Software can make the most of a marginal link — recovering lost
fragments, keeping partial messages intelligible, not letting another
process steal the port — but it cannot make a weak signal strong. When
messages stop arriving at a distance, in order of effect:

1. **Antennas.** The small stock antennas are the weakest part of the
   link. A proper 868 MHz whip, vertical, clear of the body and the
   metal of the board, is worth more than anything below. Status shows
   the signal of the last packet heard; watch it as you walk away.
2. **Height and line of sight.** Hills, buildings and people absorb
   868 MHz. Holding the radio up helps more than it looks like it should.
3. **Air rate.** The one setting that buys real distance. `radio.air_speed`
   ships at 9600; each halving buys roughly 3 dB, so **2400 hears signals
   about 6 dB weaker** — roughly twice the distance in the open, or one
   more wall or hill in town. 2400 is also the module's factory setting,
   the one its 7 km open-field figure is measured at. The price is
   airtime: four times as much per message.

   | `--range` | air rate | reach vs 9.6k | 10 s at Clear (3200) | at Balanced (1600) | at 700C |
   |---|---|---|---|---|---|
   | `normal` | 9600 | — | 7 s air, ~5 an hour | 3.5 s, ~10 | 1.7 s, ~20 |
   | `long` | 2400 | +6 dB | ~20 s air, ~1–2 an hour | ~10 s, ~3–4 | ~5 s, ~7 |
   | `longest` | 1200 | +9 dB | ~40 s: not practical | ~20 s, ~1–2 | ~9 s, ~4 |

   "An hour" is the 1% duty cycle; a message also takes that long to
   arrive. So at `long`, step voice down to Balanced or Most messages in
   Settings — Voice quality shows the count for the current air rate.

   It is a module setting, and **every radio must match**: until both are
   changed they cannot hear each other at all. On each radio, one after
   the other:

   ```bash
   cd ~/WalkieTalkie && sudo python3 provision_radio.py --range long
   ```

   That stops the Whisplay daemon for a few seconds (M0/M1 are its lines),
   writes the module, reads it back, writes `air_speed: 2400` into that
   radio's `config.yaml` — the app paces its packets and counts airtime by
   it — and starts the daemon again. Then open the app from the HAT's
   menu. `--range normal` puts it back. The installers take the same
   option: `./install-orangepi-zero2w.sh --range long`.

`transmit power` is already the module's maximum, 22 dBm.

**Measure before and after.** The [Range test](#range-test) logs where
the link gives out; run it once at 9.6k and once at 2.4k over the same
walk, and compare.

## Range test

A temporary screen for testing at a distance: **Home → Range test** on
the radio you carry. It probes the other paired radio every 30 seconds
(`radio.range_test_seconds`), and that radio answers on its own — its
app only has to be running, on any screen.

```
RANGE TEST             to jarvis · every 30s · air 9.6k
          80%          8 of the last 10 answered
 ┌ heard here ──┐ ┌ heard there ─┐
 │ -97 dBm ▂▄▆  │ │ -104 dBm ▂▄  │   how I hear it · how it hears me
 └──────────────┘ └──────────────┘
      answered in 312 ms
 sent 24 · answered 20 · marks 3 · 12:30
     range-20260929-101500.csv
```

| | |
|---|---|
| **1 click** | mark this spot: a numbered row in the log (and a chirp) |
| **2 clicks** | stop; the log is kept |
| **3 clicks** | probe now, without waiting |
| **hold** | talk to the radio under test, without leaving the screen |

Every probe is a row in `~/.whisplay-walkie/rangetest/range-<date>.csv`:
time, answered or not, round trip, signal both ways, and how many probes
the other radio has heard (so a lost *answer* can be told from a lost
*probe*). Marks, and voice sent and received during the test with any
fragments lost, are rows too. Rows are written as they happen, so a
flat battery at the far end loses nothing. To read them:

```bash
scp orangepi@192.168.0.130:.whisplay-walkie/rangetest/*.csv .
python3 tools/range_report.py range-*.csv
```

```
== range-20260929-101500.csv
   whole test    20/24 answered ( 83%)  here median -97, worst -118 ...  there ...
     start         4/4  answered (100%)  ...
     after mark 1  8/8  answered (100%)  ...
     after mark 2  6/8  answered ( 75%)  ...
     after mark 3  2/4  answered ( 50%)  ...
   link first gave out (3 unanswered in a row) at 612.4 s, after mark 3
```

The probes spend the duty cycle like anything else: at 1%, one every
30 s is about half of each radio's hour (the answers are the other
radio's half), which leaves room for a few voice tests. A probe that
would not fit is skipped and logged as such rather than delayed.
Leaving the screen stops the test.

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
python3 -m pytest tests -q     # 554 tests, no hardware required
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
| Garbled or one-way traffic | Login console on the port | Step 3; on an Orange Pi, `./install-orangepi-zero2w.sh` and reboot |
| `fuser -v /dev/ttyS0` shows `bash` (Orange Pi) | Auto-login shell on the console | `./install-orangepi-zero2w.sh` and reboot |
| `write failed: [Errno 5] Input/output error` in the log | The port was hung up under the app — a login shell on it restarting | The app now reopens the port; to stop it happening, `./install-orangepi-zero2w.sh` and reboot |
| `LoRa port shared: run ./setup.sh` on screen | Something else has the LoRa port open, or the kernel console is on it | Run the board's installer and reboot |
| `radio busy: quit Messenger` on screen | The [Messenger](../Messenger) has the radio: both apps lock the port, and the second one is refused | Open the Messenger, quit it (four clicks), then open WalkieTalkie again |
| Voice breaks up, or is noise after a point | Fragments lost; before this version a lost fragment garbled the rest | Update; see [When fragments go missing](#when-fragments-go-missing) |
| Messages stop arriving at a distance | Signal below the module's sensitivity | See [Range](#range) |
| `to talk: Home > Start` when holding | A hold only talks inside Start | Home → Start, then hold |
| `holds M0/M1 ... Run this with sudo` | `provision_radio.py` run without sudo | `sudo python3 provision_radio.py …` |
| `cannot take M0/M1` when provisioning | Something still holds the lines | Quit the app (four clicks) and retry |
| Both radios `disconnected` right after changing `--range` | Only one module changed so far | Provision the other radio with the same `--range` |
| `jarvis disconnected` while walking | Out of range (or its app closed) | Walk back until `back in range`; see [Range](#range) |
| `keys changed: pair again` | That radio was reset, or paired elsewhere | Home → Pair devices on both |
| A message shows `(gaps · play to fix)` | Fragments lost at the edge of range | Receive, three clicks: the missing parts are asked for |
| `no answer from jarvis` after replaying | The sender is out of range, off, or no longer has it | Try again once back in range |
| `radio offline` on screen | Port busy or HAT unseated | `fuser -v /dev/ttyS0` |
| Nothing received | Frequency or air-rate mismatch | `provision_radio.py --check` on both |
| Two radios cannot hear each other at all | Different privacy channels, or versions | Same channel in Settings; update both |
| `pair with X first` | A contact from before encryption | Pair again: Home → Pair devices |
| Pairing lists nobody | The other radio's module is in configuration mode (no DC fix), or it is not on its Pair screen | Run that board's installer; open Pair devices on both |
| `radio in setup mode: run the installer` | The module answers `FF FF FF`: M1 (the LCD's DC line) is held high | Run the board's installer, which applies the DC fix |
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

## Recent changes

### Sharing a board with the Messenger

#### Update summary
- WalkieTalkie and the [Messenger](../Messenger) can be installed on the
  same radio without breaking each other. Both apps now lock the LoRa
  port. Whichever starts second is refused and names the one to quit,
  instead of each quietly getting half the bytes.

#### What changed
- [app/radio/sx126x.py](app/radio/sx126x.py) opens the port with
  `exclusive=True` (an `flock`). A port that is already locked raises
  `PortBusy` and names the holder by its app folder (`port_users`). The
  Messenger's driver got the same change.
- The screen says **radio busy: quit Messenger** (for 10 s, then under
  Settings as the radio mode). Starting a call, pairing or a range test
  without a radio says the same, not just "radio offline".
- Why it was possible: the Whisplay desktop starts an app without
  stopping the one before, and WalkieTalkie keeps its radio when it
  loses the screen, so it can keep listening. An app started over SSH or
  at boot counts too. The start-up check only logged "LoRa port shared"
  and carried on sharing.
- The Messenger now reads the frequency and air rate from this
  `config.yaml` (`auto`). So `provision_radio.py --range …` here moves
  both apps at once. Its own provisioning tool refuses to write anything
  else.
- The Messenger also used to hand the desktop back with the backlight at
  80%. That is PWM on M0, so a WalkieTalkie still running in the
  background went deaf. It now hands it back at 100%, as WalkieTalkie
  does.

#### Validation
- `python3 -m pytest tests -q`: **557 passed** (3 new, in
  `test_serial_recovery.py`):
  - a second opener refused until the first closes;
  - a real child process holding a pty, named "Messenger" by its folder;
  - the app's *radio busy: quit Messenger*.
- On the Pi Zero 2 W, with the Messenger holding `/dev/ttyS0`, this
  driver was refused with `/dev/ttyS0 is in use by Messenger`.
- Both radios hold 2400 bps now: the `--range long` change from the
  previous entry is done, and both `config.yaml` files say
  `air_speed: 2400`. The Messenger exchanged messages both ways at that
  rate.

#### Notes
- The holder can be named only when both processes run with the same
  user and group, as two apps started from the HAT desktop do.
  Otherwise the screen says "quit the other app".

### In range or not, fetching a message again, more range, and a range test

#### Update summary
- Each paired radio is now shown as **in range**, **weak signal** or
  **disconnected**, with the signal both ways, from a small encrypted
  ping every two minutes. Dropping out and coming back are announced.
- A voice message that arrived with gaps can be **completed later**:
  replaying it asks the sender for the missing parts, which it answers
  from the copy it now keeps of everything it sends. A message missed
  outright is fetched once the radios are back in range.
- **More range**: `provision_radio.py --range long` sets the module to
  2.4k air (about +6 dB, roughly twice the distance in the open), now on
  the Orange Pi as well as the Pi, and keeps `config.yaml` in step.
- **Home → Range test**, a temporary screen that probes the other radio
  on a timer and logs every answer to CSV for analysis afterwards, with
  `tools/range_report.py` to summarise the logs.

#### What changed
- Protocol: four new sealed message types — `ping`, `pong`, `retrieve`
  and `resent`. Every radio has to be updated; older versions ignore
  them, but will not answer them either.
- [app/radio/linkcheck.py](app/radio/linkcheck.py) works out each radio's
  state; the paired list, Talk and Home show it. Opening Talk on a radio
  not heard in the last minute probes it first. See
  [In range, or not](#in-range-or-not).
- The link waits up to 20 ms for the signal-strength byte the module
  sends just after each packet. It usually missed the read that completed
  the packet, so a lone packet had no reading, and the byte was credited
  to the next one.
- Sent voice is kept, with its message number, in Receive's store; the
  receiver keeps which fragments never arrived. See
  [When fragments go missing](#when-fragments-go-missing), items 4–5.
- Repair waits now scale with the air rate. They assumed 9.6k, so at
  2.4k the second request went out before the first answer could arrive.
- [provision_radio.py](provision_radio.py) drives M0/M1 through libgpiod
  on both boards ([app/radio/modelines.py](app/radio/modelines.py)),
  stops and restarts the Whisplay daemon itself, quits the app, and
  writes `air_speed` into `config.yaml`. `--range normal|long|longest`;
  the installers take `--range` too. `tools/provision.sh` now just calls
  it.
- [app/rangetest.py](app/rangetest.py), the Range test screen, and
  [tools/range_report.py](tools/range_report.py). See [Range test](#range-test).
- `config.yaml`: `radio.link_check_seconds: 120`,
  `radio.range_test_seconds: 30`.

#### Validation
- `python3 -m pytest tests -q`: **554 passed** on the development
  machine, the Orange Pi Zero 2W and the Pi Zero 2 W. New tests:
  `test_linkcheck.py` (ping and pong formats, every state and transition,
  a pong over fake radios, no pong when the duty cycle is spent, keys
  that no longer open, the late signal byte), `test_retrieve.py` (the
  request, waiting for part of a message, a gap filled over fake radios
  after repair failed, refusals, the inbox merge, replay → fetch → play,
  a missed message fetched whole, at most twice, only the sender's own
  copy of the message meant), `test_rangetest.py` (the CSV, success
  rate, skipped probes, the report, the screen's gestures, a hold that
  talks without leaving it), `test_provision.py` (each board's lines,
  libgpiod 1.x and 2.x, config mode and back, `config.yaml` kept intact),
  `test_link_status_ui.py` (screens, announcements, when checks go out).
- **Over the air, both radios on one desk**, with the apps paused and a
  script using the real keys and link on each:
  - The Orange Pi's probe was answered in 384 ms: −57 dBm heard here,
    −58 dBm heard there.
  - A four-fragment voice message went out with fragment 1 withheld and
    its repair copy dropped. It arrived with `missing [1]`; the Pi asked
    for it, the Orange Pi sent it, and the rebuilt message matched the
    original byte for byte (same SHA-256).
  - On the Pi all five packets' signal bytes arrived late — before this
    change, every one of those readings would have gone to the wrong
    packet.
- Both apps relaunched from the daemon: each logged the other
  `not checked yet -> in range` within seconds, and sends its pings; no
  errors.
- `provision_radio.py` run without sudo on both boards: it detects the
  board and its lines (Orange Pi: 261/227 on gpiochip0, which `gpioinfo`
  shows held by `whisplay`; Pi: 22/27) and stops, asking for sudo. The
  libgpiod calls were checked against the real bindings on each board.
- New screens rendered to images and checked by eye.

#### Notes
- **Not yet done on the hardware: the air rate change itself.** It needs
  sudo, so run it on each radio, one after the other:
  `ssh -t orangepi@192.168.0.130 'cd WalkieTalkie && sudo python3 provision_radio.py --range long'`,
  then the same on `jarvis@192.168.0.33`. Until both are done, they
  cannot hear each other. Then set Voice quality to Balanced or Most
  messages: Clear takes ~20 s of air per 10 s message at 2.4k.
- Range test first at 9.6k, then at 2.4k over the same walk, to see what
  the change is worth where you use the radios.
- Distance was not tested: both radios are on one desk. Disconnected and
  back-in-range were tested in software only.
- The range test is meant to be temporary; it is one row on Home and one
  module, easy to remove once the testing is done.

### Crashes, broken-up voice and range (Orange Pi and Pi)

#### Update summary
- The Orange Pi's "crashes" were its LoRa port being hung up by an
  auto-login shell on the same port; broken-up voice was lost fragments
  garbling everything after them; messages lost at a distance were
  fragments never recovered, or a lost last fragment never delivered.
  All three are fixed in the app, and the Orange Pi installer now
  actually moves the console off the port.

#### What changed
- **Separate installers per board**: `install-raspberrypi.sh` and
  `install-orangepi-zero2w.sh`; `setup.sh` picks one.
- **Orange Pi console**: its `boot.cmd` puts `console=ttyS0` on the
  command line for `console=display` too, so the installer now uses
  `console=none` + `extraargs=console=tty1` there, and masks
  `serial-getty@ttyS0`.
- **Serial port recovery**: a hung-up port (`[Errno 5] Input/output
  error`) is reopened instead of leaving the radio dead until restart.
  The app names anything else on the port, on screen and in the log.
- **Lost fragments**: voice fragments carry 168 bytes (whole Codec2
  frames); a lost one keeps its place and plays as silence; the receiver
  asks for missing fragments and the sender resends them; a lost last
  fragment is delivered by the link's own timer. See
  [When fragments go missing](#when-fragments-go-missing).
- **Hold-to-talk** only inside Start (Start, Paired, Talk). Receive is
  listen-only. The microphone is kept warm only where a hold can talk.
- **Range** advice written down: [Range](#range).
- **Pairing could not find the other radio.** Two causes, both fixed:
  the Orange Pi's Whisplay driver lacked the DC fix, so its module sat in
  configuration mode, sending and hearing nothing; and the two radios
  were on different privacy channels. The installers now check for and
  apply the DC fix (the patch file in `docs/` was malformed and has been
  regenerated from the working Pi), the app warns when the module
  answers `FF FF FF`, pairing is heard on every channel, and the radio
  that asks joins the channel of the one that accepts.

#### Validation
- `python3 -m pytest tests -q`: 450 passed on the development machine,
  on the Orange Pi Zero 2W (Ubuntu 22.04, Python 3.10) and on the Pi
  Zero 2 W (Debian 13, Python 3.13).
- New tests: `test_repair.py` (lossy fake radios: repair, lost last
  fragment, unrecoverable gap, two listeners, sealed), `test_voice_gaps.py`
  (a gap decodes to silence, the rest byte-for-byte),
  `test_serial_recovery.py` (hung-up port, shared-port detection),
  hold-to-talk tests in `test_menu.py` and `test_navigation.py`.
- Evidence behind the fixes: the Orange Pi log (40 tracebacks, all
  `[Errno 5]` on `/dev/ttyS0`; `/proc/cmdline` still `console=ttyS0`
  after `console=display`); received clips on the Pi (every incomplete
  one misaligned — 135, 159, 193 bytes against 4-byte frames — while
  levels were healthy, −17 to −27 dBFS, no clipping).
- The installer's console logic was replayed against the Orange Pi's own
  `boot.cmd` and `orangepiEnv.txt`, including a second run (no change).
- Both apps start clean on the devices; the Orange Pi's reports the
  shared port until its installer has run.
- Radio check between the two boards, same channel, unencrypted beacons
  once a second for 25 s: before the DC fix the Pi heard nothing and the
  Orange Pi's own module answered `FF FF FF` to each send; after it, the
  Orange Pi heard 24 of 25 and the Pi 25 of 25, at −67 dBm.
- After rebooting the Orange Pi: `/proc/cmdline` has `console=tty1`
  only, nothing holds `/dev/ttyS0`, and both apps start without warnings.
  454 tests pass on all three machines.

#### Notes
- A new Orange Pi needs `./install-orangepi-zero2w.sh` once (it needs
  sudo) and a reboot; the one used here has had both.
- Radio-to-radio behaviour (pairing, repair over real RF, range) has to
  be checked by hand with both devices: it cannot be driven from SSH.
- Range beyond this needs better antennas or a lower air rate; see
  [Range](#range).

### Robotic, hard-to-understand voice

#### Update summary
- Voice sounded synthetic and unclear. Two causes, measured with STOI on
  recorded speech: the 700C codec (0.73 at best), and ALSA's unfiltered
  conversion between the cards' 48 kHz and Codec2's 8 kHz, which alone
  cost enough that no codec could sound clear through it. Together:
  0.67. Now: 0.87 by default.

#### What changed
- Capture and playback open the cards at 48 kHz; `app/audio/dsp.py`
  converts with a proper filter (polyphase, numpy only), then high-passes
  at 120 Hz and levels speech to −9 dBFS before encoding.
- Codec2 3200 is the default; **Settings → Voice quality** offers Clear
  (3200), Balanced (1600) and Most messages (700C), and shows about how
  many ten-second messages an hour each allows.
- The shipped `config.yaml` sets `codec_mode: "3200"`.

#### Validation
- STOI grid on concatenated human speech (ALSA's sample clips), every
  codec mode, ALSA-linear against filtered conversion, input levels −6 to
  −30 dBFS, with and without levelling and high-pass; the levelling
  target (−9 dBFS) was chosen from it. Scoring used a fixed per-mode
  codec delay, after a first pass with per-clip alignment turned out to
  produce spurious drops.
- `tests/test_dsp.py`: pass band within 0.5 dB, a 6 kHz tone kept out of
  the voice band by more than 50 dB (where plain interpolation lets it
  in), no mirror image on the way up, exact lengths, levelling and its
  20 dB cap, hum removal, and the polyphase conversion identical to plain
  filtering sample for sample. Capture and playback tests check the
  48 kHz rates. 469 tests pass.
- Speed on the boards, 20 s clip: down 0.3–0.5 s, levelling 0.3 s, up
  0.4–0.5 s (1.4 s each way before the polyphase rewrite).

- **Pi → Orange Pi still unclear, Orange Pi → Pi fine.** Both directions
  arrived complete (3200, no gaps, −55 to −71 dBm), so not the radio.
  Decoded, the Pi's speech had 4–5 dB more energy below 300 Hz and
  2.5 dB less at 1–2 kHz than the Orange Pi's. The one difference in the
  chain: the Pi's `mic` control at 100% (+29 dB boost) against the
  Orange Pi's 80% (+20 dB). The app now sets 80% at start, on both.

#### Notes
- Not measured through a real microphone and speaker: the radios were in
  use. Listen to a message each way at Clear and at Balanced.
- Clear (3200) takes four times the airtime of 700C: five ten-second
  messages an hour at 1%. Step down if "duty cycle full" appears.

## Licence and credits

Radio register layout derived from Waveshare's SX126X HAT sample code.
Speech coding by [Codec2](https://www.rowetel.com/codec2.html) (David
Rowe, LGPL). Display and button access through the Whisplay daemon.
