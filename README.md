# photogrep

Extract images from encrypted iOS backups and search them with natural language using CLIP.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Extract images from an iOS backup
python -m src extract --backup-path /path/to/backup

# Search extracted images
python -m src search "sunset at the beach" --output output_images/Device_Name

# Launch the GUI
python -m src gui
```

## Commands

| Command   | Description                          |
|-----------|--------------------------------------|
| `extract` | Extract images from iOS backup       |
| `index`   | Build/rebuild the CLIP search index  |
| `search`  | Search images by text query          |
| `gui`     | Launch the photo gallery UI          |
# photogrep
