#!/usr/bin/env bash
#
# Fire & Rescue Academy Discord Bot — one-command installer for
# Raspberry Pi (Debian Bookworm) and other Debian-based systems.
#
# Fresh install (interactive):
#   bash <(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/install.sh)
#
# Or from a local checkout:
#   ./install.sh
#
# Re-running the script updates the bot (git pull + dependencies +
# service restart) and leaves your config.yaml/.env untouched.
#
# Optional environment overrides:
#   FRA_BRANCH=main            # git branch to install
#   FRA_DIR=~/FireAndRescueAcademyDiscordBot   # install location
#
# Uninstall (keeps data/ and config):
#   ./install.sh uninstall

set -euo pipefail

REPO_URL="https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot.git"
BRANCH="${FRA_BRANCH:-main}"
INSTALL_DIR="${FRA_DIR:-$HOME/FireAndRescueAcademyDiscordBot}"
SERVICE_NAME="fra-bot"
PYTHON_MIN_MINOR=11   # Python 3.11+

BOLD=$(tput bold 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

say()  { echo "${GREEN}==>${RESET} ${BOLD}$*${RESET}"; }
warn() { echo "${YELLOW}==>${RESET} $*"; }
die()  { echo "${RED}FOUT:${RESET} $*" >&2; exit 1; }

# Prompts must work even when the script is piped into bash.
ask() {
    local prompt="$1" default="${2:-}" answer
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " answer < /dev/tty || true
        echo "${answer:-$default}"
    else
        read -r -p "$prompt: " answer < /dev/tty || true
        echo "$answer"
    fi
}

ask_secret() {
    local prompt="$1" answer
    read -r -s -p "$prompt: " answer < /dev/tty || true
    echo "" > /dev/tty
    echo "$answer"
}

ask_number() {
    local prompt="$1" default="${2:-0}" answer
    while true; do
        answer=$(ask "$prompt" "$default")
        [[ "$answer" =~ ^[0-9]+$ ]] && { echo "$answer"; return; }
        warn "Voer een getal in (of Enter voor $default)." > /dev/tty
    done
}

require_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "Draai dit script als gewone gebruiker (bijv. 'pi'), niet als root. Sudo wordt gebruikt waar nodig."
    fi
}

# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------

do_uninstall() {
    say "Service stoppen en verwijderen…"
    sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
    warn "De map $INSTALL_DIR (incl. database en config) is NIET verwijderd."
    warn "Verwijder die zelf met: rm -rf $INSTALL_DIR"
    say "Klaar."
}

# ----------------------------------------------------------------------
# System dependencies
# ----------------------------------------------------------------------

install_system_deps() {
    say "Systeempakketten controleren (python3, venv, git)…"
    local missing=()
    command -v git >/dev/null || missing+=(git)
    command -v python3 >/dev/null || missing+=(python3)
    python3 -c "import venv" 2>/dev/null || missing+=(python3-venv)
    python3 -c "import ensurepip" 2>/dev/null || missing+=(python3-venv)

    if [ ${#missing[@]} -gt 0 ]; then
        say "Installeren via apt: ${missing[*]} (sudo-wachtwoord kan gevraagd worden)"
        sudo apt-get update -qq
        sudo apt-get install -y -qq "${missing[@]}"
    fi

    local minor
    minor=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$(python3 -c 'import sys; print(sys.version_info.major)')" -lt 3 ] || [ "$minor" -lt "$PYTHON_MIN_MINOR" ]; then
        die "Python 3.${PYTHON_MIN_MINOR}+ is vereist (gevonden: $(python3 --version)). Debian Bookworm levert 3.11."
    fi
}

# ----------------------------------------------------------------------
# Code + virtualenv
# ----------------------------------------------------------------------

fetch_code() {
    # When run from inside a checkout, use that checkout.
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
    if [ -n "$script_dir" ] && [ -d "$script_dir/fra_bot" ]; then
        INSTALL_DIR="$script_dir"
        say "Bestaande checkout gevonden: $INSTALL_DIR"
        return
    fi

    if [ -d "$INSTALL_DIR/.git" ]; then
        say "Bestaande installatie bijwerken in $INSTALL_DIR (branch: $BRANCH)…"
        git -C "$INSTALL_DIR" fetch origin "$BRANCH"
        git -C "$INSTALL_DIR" checkout "$BRANCH"
        git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
    else
        say "Repository klonen naar $INSTALL_DIR (branch: $BRANCH)…"
        git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
}

install_python_deps() {
    say "Python-omgeving opzetten…"
    cd "$INSTALL_DIR"
    if [ ! -d .venv ]; then
        python3 -m venv .venv
    fi
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
}

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

configure() {
    cd "$INSTALL_DIR"

    if [ -f .env ] && [ -f config.yaml ]; then
        say "Bestaande config.yaml en .env gevonden — configuratie wordt overgeslagen."
        warn "Opnieuw configureren? Verwijder config.yaml en/of .env en draai de installer nogmaals."
        return
    fi

    echo ""
    echo "${BOLD}--- Configuratie ---${RESET}"
    echo "Deze gegevens worden alleen lokaal opgeslagen ($INSTALL_DIR)."
    echo "Kanaal-ID's mag je nu leeg laten (Enter = uitgeschakeld) en later"
    echo "invullen in config.yaml. Discord Developer Mode aanzetten om ID's"
    echo "te kunnen kopiëren (rechtsklik op kanaal > Copy Channel ID)."
    echo ""

    if [ ! -f .env ]; then
        local discord_token mc_email mc_password
        while true; do
            discord_token=$(ask_secret "Discord bot-token")
            [ -n "$discord_token" ] && break
            warn "Token mag niet leeg zijn." > /dev/tty
        done
        while true; do
            mc_email=$(ask "MissionChief login e-mail")
            [ -n "$mc_email" ] && break
            warn "E-mail mag niet leeg zijn." > /dev/tty
        done
        while true; do
            mc_password=$(ask_secret "MissionChief wachtwoord")
            [ -n "$mc_password" ] && break
            warn "Wachtwoord mag niet leeg zijn." > /dev/tty
        done

        umask 177
        cat > .env <<EOF
DISCORD_TOKEN=$discord_token
MC_EMAIL=$mc_email
MC_PASSWORD=$mc_password
EOF
        umask 022
        chmod 600 .env
        say ".env geschreven (chmod 600)."
    fi

    if [ ! -f config.yaml ]; then
        local alliance_id guild_id ch_admin ch_apps ch_members ch_logs ch_reports
        alliance_id=$(ask_number "MissionChief alliance-ID" "1621")
        guild_id=$(ask_number "Discord server (guild) ID" "0")
        ch_admin=$(ask_number "Kanaal-ID: admin log (bot-fouten/health)" "0")
        ch_apps=$(ask_number "Kanaal-ID: nieuwe applications" "0")
        ch_members=$(ask_number "Kanaal-ID: member events (join/leave)" "0")
        ch_logs=$(ask_number "Kanaal-ID: alliance logs feed" "0")
        ch_reports=$(ask_number "Kanaal-ID: daily/monthly rapporten" "0")

        sed \
            -e "s/^  alliance_id: .*/  alliance_id: $alliance_id/" \
            -e "s/^  guild_id: .*/  guild_id: $guild_id/" \
            -e "s/^    admin_log: .*/    admin_log: $ch_admin/" \
            -e "s/^    applications: .*/    applications: $ch_apps/" \
            -e "s/^    member_events: .*/    member_events: $ch_members/" \
            -e "s/^    alliance_logs: .*/    alliance_logs: $ch_logs/" \
            -e "s/^    reports: .*/    reports: $ch_reports/" \
            config.example.yaml > config.yaml
        say "config.yaml geschreven."
    fi
}

# ----------------------------------------------------------------------
# systemd service
# ----------------------------------------------------------------------

install_service() {
    say "systemd-service installeren ($SERVICE_NAME)…"
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"
    sudo tee "$unit" > /dev/null <<EOF
[Unit]
Description=Fire & Rescue Academy Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m fra_bot
Restart=on-failure
RestartSec=30

# Keep the Pi healthy: cap memory, lower priority slightly.
MemoryMax=512M
Nice=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
}

start_service() {
    say "Bot starten…"
    sudo systemctl restart "$SERVICE_NAME"
    sleep 4
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        say "De bot draait! 🚒"
    else
        warn "De service is niet actief. Laatste logregels:"
        sudo journalctl -u "$SERVICE_NAME" -n 25 --no-pager || true
        die "Start mislukt — controleer je token/wachtwoord in $INSTALL_DIR/.env en draai: sudo systemctl restart $SERVICE_NAME"
    fi
}

summary() {
    echo ""
    echo "${BOLD}--- Klaar ---${RESET}"
    echo "Installatiemap : $INSTALL_DIR"
    echo "Logs volgen    : journalctl -u $SERVICE_NAME -f"
    echo "Herstarten     : sudo systemctl restart $SERVICE_NAME"
    echo "Stoppen        : sudo systemctl stop $SERVICE_NAME"
    echo "Config wijzigen: $INSTALL_DIR/config.yaml (daarna herstarten)"
    echo "Updaten        : draai dit script opnieuw"
    echo ""
    echo "In Discord: gebruik !fra status om de sync-status te zien."
    echo "De expenses-backfill (3150+ pagina's) loopt automatisch in ±1,5 dag."
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

main() {
    if [ "${1:-}" = "uninstall" ]; then
        require_not_root
        do_uninstall
        exit 0
    fi

    echo "${BOLD}Fire & Rescue Academy Discord Bot — installer${RESET}"
    require_not_root
    command -v systemctl >/dev/null || die "systemd is vereist (Raspberry Pi OS / Debian)."
    install_system_deps
    fetch_code
    install_python_deps
    configure
    install_service
    start_service
    summary
}

main "$@"
