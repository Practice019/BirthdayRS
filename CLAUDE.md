# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BirthdayRS is a birthday reminder system that supports both lunar (Chinese) and solar (Gregorian) calendar birthdays. It sends notifications via email and ServerChan, providing rich Chinese traditional culture information including zodiac signs, GanZhi (干支), festivals, and solar terms.

## Common Commands

### Development
```bash
# Install dependencies — deps are grouped by extras (see pyproject.toml)
uv sync --all-extras           # everything (desktop + web + dev + docs)
uv sync --extra dev --extra web  # what CI installs

# Run the desktop app (native window, no port)
uv run python -m src.main app --config config.yml

# Run the web admin UI (binds a port)
uv run python -m src.main web --config config.yml

# Run the daily check once
uv run python -m src.main run --config config.yml

# Preview email template
uv run python -m src.main preview

# Validate configuration
uv run python -m src.main validate --config config.yml

# Show application info
uv run python -m src.main info --config config.yml
```

### Dependency extras

`pywebview` lives in the `desktop` extra, **not** in core dependencies: it pulls in
`pythonnet`, which is Windows-only and cannot be installed in a Linux container or CI
runner. Core deps are pure-Python except PyYAML, so the container path stays light.

| extra | contents | needed by |
|---|---|---|
| (core) | jinja2, lunar_python, httpx, aiosmtplib, ruamel.yaml, click | CLI `run` / container |
| `desktop` | pywebview | `src.main app` |
| `web` | fastapi, uvicorn, python-multipart | `src.main web` |
| `dev` | pytest, pytest-cov, pytest-html, flake8, psutil | tests |
| `docs` | sphinx, furo, myst-parser | documentation |

### Testing
```bash
# Run all tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src

# Run tests with HTML coverage report
uv run pytest --cov=src --cov-report=html

# Run specific test file
uv run pytest tests/test_main.py
```

### Code Quality
```bash
# Run flake8 linter
uv run flake8 .

# Flake8 configuration is in .flake8
# - Max line length: 100
# - Ignores: E501, W503
```

### Docker
```bash
# Build Docker image
docker build -t birthdayrs .

# Run with Docker
docker run -v ${PWD}/config.yml:/app/config.yml birthdayrs run

# Preview with Docker
docker run -v ${PWD}/config.yml:/app/config.yml -v ${PWD}/previews:/app/previews birthdayrs preview
```

## Architecture

### Core Components

**BirthdayReminder** (`src/main.py`)
- Main application entry point
- Orchestrates birthday checking and notification sending
- Uses asyncio for concurrent notification delivery
- CLI interface built with Click

**ConfigManager** (`src/core/config_manager.py`)
- Handles YAML config loading and validation
- Provides default config path resolution
- Returns templates directory path

**BirthdayChecker** (`src/core/checker.py`)
- Checks birthdays against current date with advance reminder support
- Handles both solar and lunar calendar conversions using `lunar_python` library
- Generates rich date information: GanZhi, zodiac, festivals, solar terms, constellations
- Returns tuple of (Recipient, is_birthday, extra_info) for each recipient

**NotificationFactory** (`src/core/notification_factory.py`)
- Factory pattern for creating notification senders
- Supports multiple notification types: `email` (SMTP), `resend` (HTTP API), `serverchan`
- Creates senders based on config.notification_types list

**NotificationBase** (`src/notification/notification_base.py`)
- Abstract base class for all notification senders
- Defines interface: `render_content()` and `send()`

**EmailSender** (`src/notification/sender_email.py`)
- Sends HTML emails via SMTP using aiosmtplib
- Uses Jinja2 templates from `templates/` directory
- Includes retry decorator with exponential backoff (3 retries, 1s initial delay, 2x backoff)
- Preview functionality generates HTML files in `previews/` directory

**ServerChanSender** (`src/notification/sender_serverchan.py`)
- Sends notifications via ServerChan API
- Uses httpx for async HTTP requests

### Configuration Flow

1. ConfigManager loads `config.yml` (or path from `--config` option)
2. Config.from_yaml() parses YAML and applies defaults:
   - SMTP defaults: `default_receive_email`, `default_template_file`, `default_reminder_days`
   - ServerChan defaults: `default_sckey`, `default_reminder_days`
3. Recipients inherit defaults if not explicitly set
4. NotificationFactory creates senders based on `notification_types` list

### Birthday Check Flow

1. BirthdayChecker iterates through recipients
2. For each recipient, checks dates from today to (today + reminder_days)
3. Compares solar_birthday (month/day match) and lunar_birthday (using lunar_python)
4. Extracts rich date info: GanZhi, zodiac, festivals, solar terms, week, constellation
5. Returns results with extra_info dict containing all date metadata

### Notification Flow

1. BirthdayReminder.run() calls check_birthdays()
2. For each birthday match, creates async task for send_birthday_reminder()
3. Tasks run concurrently via asyncio.gather()
4. Each notification sender:
   - Renders Jinja2 template with recipient name and extra_info
   - Sends via specific channel (email/serverchan)
   - Retries on failure (email only)

## Configuration Structure

The `config.yml` must include:
- `notification.smtp` - SMTP server settings for email
- `notification.serverchan` - ServerChan API key
- `notification.start_notification` - Comma-separated list: "email", "serverchan", or "email,serverchan"
- `recipients` - List of recipients with:
  - `name` (required)
  - `solar_birthday` and/or `lunar_birthday` (at least one required, format: YYYY-MM-DD)
  - `email` (optional if default_receive_email set)
  - `reminder_days` (optional, defaults from smtp/serverchan config)
  - `template_file` (optional, defaults to birthday.html)

## Important Notes

- **`lunar_birthday` stores the LUNAR year/month/day, not a solar date.** e.g. `1989-12-24`
  means 农历腊月廿四. `BirthdayChecker` compares its month/day directly against the lunar
  calendar. Do not "convert" it — an earlier `config.example.yml` comment claimed it was the
  solar equivalent, which was wrong and made people fill in the wrong dates.
- **`solar_birthday` is the source of truth** for the UI. The web/desktop UI derives the
  lunar date from it; a hand-written `lunar_birthday` that disagrees is flagged in the list.
- Leap months (`闰月`) are stored as the absolute month number, so those people get a
  reminder every year. Documented tradeoff — see DESIGN.md.
- The lunar_python library handles conversion between solar and lunar calendars. Validate
  solar input with `datetime.date` first: `Solar.fromYmd` silently normalizes `1990-01-32`
  into 1990-02-01 instead of raising.
- Notifications go to the **user** (`default_receive_email`), never to the birthday person.
- Templates use Jinja2 and are located in `templates/` directory. `templates/birthday.html`
  is the email body; edit the file directly (the UI no longer exposes a template editor).
- Email retry logic uses exponential backoff to handle transient SMTP failures
- All notification senders run concurrently for performance
- The application logs to both stdout and `birthday_reminder.log` (in the current working
  directory, so containers should mount that path)
- The desktop app logs to `~/.birthdayrs/app.log`
- Tests use pytest with async support (pytest-asyncio, STRICT mode — async fixtures need
  `pytest_asyncio.fixture`)
- CI/CD pipeline runs on GitHub Actions: lint → test → docker build/push
- Python version: 3.10+ (specified in pyproject.toml as >=3.8, but CI uses 3.10)
