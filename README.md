# rtl-airwave-scanner

Python RTL-SDR scanner for **SIGINT-style** capture:

- Wideband survey + dwell when energy appears
- Records **UTC time, frequency, SNR, audio WAV**
- Web viewer with **squelch sliders** and **band group toggles** (ATC / ham / GMRS / …)
- Site-specific ATC, AWOS ignores, and local repeaters live in **`site.yaml`** (not committed)

## Requirements

- Linux
- RTL-SDR dongle (RTL2832U + R820T/R820T2, etc.)
- Python 3.10+
- `librtlsdr` (`sudo apt install librtlsdr0` or build from osmocom)

Blacklist kernel DVB modules if the stick is claimed as a TV tuner (see project notes / distro docs).

## Quick start

```bash
git clone https://github.com/Bobpick/rtl-airwave-scanner.git
cd rtl-airwave-scanner

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Base config (generic)
cp config.example.yaml config.yaml

# YOUR location: ATC freqs, local repeater, AWOS to ignore
cp site.example.yaml site.yaml
# edit site.yaml

./run.sh          # scanner
./view.sh         # http://127.0.0.1:8765/
```

## Site config (`site.yaml`)

Keep personal/airport data out of the main config:

| Field | Purpose |
|--------|---------|
| `known_channels` | Labels (CTAF, approach, your repeater) |
| `bands` | Extra priority scan windows |
| `ignored_frequencies_hz` | AWOS / birdies never to record |

Base file: `config.yaml` (or `config.example.yaml`).  
Overlay: `site.yaml` (gitignored). Template: `site.example.yaml`.

Frequencies are **Hz** (`122.725 MHz` → `122725000`).

## Band groups (viewer)

| Toggle | Typical content |
|--------|------------------|
| **ATC** | 118–137 MHz AM (off by default) |
| **Ham** | 2 m, 70 cm, 1.25 m |
| **GMRS/FRS** | Full US GMRS/FRS table |
| **MURS** | MURS |
| **Marine** | Marine VHF |

Toggles and squelch apply live via `squelch.json`.

## Output

| Path | Content |
|------|---------|
| `recordings/*.wav` | Audio |
| `recordings/transmissions.db` | SQLite log |
| `recordings/transmissions.csv` | CSV log |

## License

MIT (see `LICENSE`).

## Legal

Receive only where lawful. Authors are not responsible for misuse.
