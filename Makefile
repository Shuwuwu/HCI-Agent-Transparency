# Makefile: the standard commands for the participant server, its port assignments and its readiness checks.
#
# Purpose:
#   One place for the server commands documented in README.md and docs/running_the_study.md, each on a fixed
#   port, so two servers never compete for a port and the rehearsal and study servers are never started without
#   passing their readiness check (preflight.py). Every target calls the existing scripts with their existing
#   options; nothing here changes what a server does.
#
# Inputs:
#   The experiment configurations in src/data/experiment_configs/ (development.json, rehearsal.json, study.json)
#   and the corpora in src/data/corpora/. Command-line variables:
#     PYTHON=...   the interpreter (default python3; 3.10 or newer)
#     NO_BACKUP=1  pass --no-backup to the development and rehearsal servers and the tunnel; refused for the study
#
# Outputs:
#   None of its own. The servers write what they always write: participant data in the configuration's own
#   database (data/participants/<environment>.sqlite3) and, unless NO_BACKUP=1, the startup and shutdown backups
#   in data/participants/backups/<environment>/.
#
# Dependencies:
#   GNU make 3.81 or newer, python3, lsof (port checks; macOS ships it), cloudflared (the tunnel target only),
#   the Tailscale app, signed in (the funnel-* targets only).
#   Scripts: participant_server.py, preflight.py, participant_store.py, rehearsal_tunnel.py, funnel_server.py.
#
# Usage:
#   make                    # list the targets and the port assignments
#   make preflight-study    # readiness check, no server started
#   make serve-study        # readiness check, then the study server on 127.0.0.1:8765 (Ctrl+C stops it)
#
# Safeguards kept here (all are also enforced by the scripts themselves):
#   - No target passes --db: each server uses the database its configuration names, so development, rehearsal
#     and study data stay in separate files.
#   - No target passes --host or --allow-network: every server listens on 127.0.0.1 only.
#   - Only the review-* targets pass --researcher-preview, always with the development configuration and on their
#     own ports; the serve-* targets, the tunnel and the funnel-* targets never do.
#   - Only tunnel and funnel-* make a server reachable from other computers, and only through their scripts, which
#     refuse to publish unless the researcher routes answer 404. tunnel refuses the study; funnel-study refuses
#     while a participant-facing placeholder is left. funnel-* always back the database up (NO_BACKUP is refused).
#   - Stop a server with Ctrl+C in its terminal. make passes the interrupt to the server and waits for it, so the
#     server writes its shutdown backup as it does when started directly.

PYTHON ?= python3
PY := $(PYTHON) -B
SCRIPTS := src/scripts
CONFIG_DIR := src/data/experiment_configs
CORPUS_DIR := src/data/corpora

# Port assignments. The participant links of every environment are printed with FLOW_PORT, so keep 8765.
FLOW_PORT := 8765
REVIEW_SIMULATED_PORT := 8771
REVIEW_STUDY_PORT := 8772
REVIEW_VALIDATION_PORT := 8773
TUNNEL_PORT := 8780

ENVIRONMENTS := development rehearsal study
BACKUP_FLAG := $(if $(filter 1 yes true,$(NO_BACKUP)),--no-backup,)

# The study database is always backed up (participant_server.py refuses --no-backup for it as well); stop before
# the readiness check runs rather than after it.
ifneq ($(filter serve-study,$(MAKECMDGOALS)),)
ifneq ($(BACKUP_FLAG),)
$(error NO_BACKUP is refused for the study: the study database is always backed up)
endif
endif
# funnel_server.py has no --no-backup: a server reachable by participants is always backed up.
ifneq ($(filter funnel-%,$(MAKECMDGOALS)),)
ifneq ($(BACKUP_FLAG),)
$(error NO_BACKUP is refused for the funnel targets: they always back the database up)
endif
endif

# $(call require_free_port,PORT): stop with a clear message if something already listens on PORT.
require_free_port = if lsof -nP -iTCP:$(1) -sTCP:LISTEN >/dev/null 2>&1; then \
	echo "Port $(1) is already in use (see 'make ports'). Stop that server with Ctrl+C in its terminal first." >&2; \
	exit 1; fi

# $(call preflight,ENVIRONMENT,PORT): the read-only readiness check; exits 1 on any FAIL, which stops make.
preflight = $(PY) $(SCRIPTS)/preflight.py --config $(CONFIG_DIR)/$(1).json --port $(2)

# $(call participant_server,ENVIRONMENT,PORT,EXTRA ARGS)
participant_server = $(PY) $(SCRIPTS)/participant_server.py --experiment-config $(CONFIG_DIR)/$(1).json \
	--port $(2) $(3)

PREFLIGHT_TARGETS := $(addprefix preflight-,$(ENVIRONMENTS))
LINKS_TARGETS := $(addprefix links-,$(ENVIRONMENTS))

.DEFAULT_GOAL := help
.PHONY: help ports $(PREFLIGHT_TARGETS) $(LINKS_TARGETS) serve-development serve-rehearsal serve-study \
	review-simulated review-study review-validation tunnel funnel-rehearsal funnel-study

help:
	@echo "Participant server commands (stop any server with Ctrl+C in its terminal):"
	@echo ""
	@echo "  make ports                  show the port assignments and what is listening on each"
	@echo "  make preflight-ENV          readiness check for ENV = development, rehearsal or study (no server)"
	@echo "  make links-ENV              print the enrolled participants' links for ENV"
	@echo ""
	@echo "  make serve-development      participant flow, development data             127.0.0.1:$(FLOW_PORT)"
	@echo "  make serve-rehearsal        readiness check, then the rehearsal flow       127.0.0.1:$(FLOW_PORT)"
	@echo "  make serve-study            readiness check, then the study flow           127.0.0.1:$(FLOW_PORT)"
	@echo ""
	@echo "  make review-simulated       researcher preview, 12 simulated unsafe items  127.0.0.1:$(REVIEW_SIMULATED_PORT)"
	@echo "  make review-study           researcher preview, study corpus               127.0.0.1:$(REVIEW_STUDY_PORT)"
	@echo "  make review-validation      researcher preview, validation fixture         127.0.0.1:$(REVIEW_VALIDATION_PORT)"
	@echo ""
	@echo "  make tunnel                 readiness check, then the remote rehearsal over a Cloudflare Quick Tunnel"
	@echo "                              (local server on 127.0.0.1:$(TUNNEL_PORT); rehearsal only)"
	@echo "  make funnel-rehearsal       readiness check, then the rehearsal published over Tailscale Funnel"
	@echo "  make funnel-study           readiness check, then the study published over Tailscale Funnel"
	@echo "                              (local server on 127.0.0.1:$(FLOW_PORT); https://<this computer>.<tailnet>.ts.net)"
	@echo ""
	@echo "Options: NO_BACKUP=1 (serve-development, serve-rehearsal and tunnel only), PYTHON=..."

# Read-only: which of the assigned ports something is listening on, and which process.
ports:
	@printf '%-6s %-36s %s\n' PORT 'ASSIGNED TO' STATUS
	@for entry in "$(FLOW_PORT)|participant flow (serve-*, funnel-*)" \
		"$(REVIEW_SIMULATED_PORT)|review-simulated" \
		"$(REVIEW_STUDY_PORT)|review-study" \
		"$(REVIEW_VALIDATION_PORT)|review-validation" \
		"$(TUNNEL_PORT)|tunnel (local rehearsal server)"; do \
		port=$${entry%%|*}; label=$${entry#*|}; \
		pid=$$(lsof -nP -t -iTCP:$$port -sTCP:LISTEN 2>/dev/null | head -n 1); \
		if [ -n "$$pid" ]; then \
			status="in use: pid $$pid $$(ps -o args= -p $$pid | sed -e 's|^[^ ]*[Pp]ython[^ ]* ||' | cut -c 1-80)"; \
		else status="free"; fi; \
		printf '%-6s %-36s %s\n' "$$port" "$$label" "$$status"; \
	done

$(PREFLIGHT_TARGETS): preflight-%:
	$(call preflight,$*,$(FLOW_PORT))

# Re-prints existing links only; enrolment (--enrol-all) stays a deliberate, separate command.
$(LINKS_TARGETS): links-%:
	$(PY) $(SCRIPTS)/participant_store.py --config $(CONFIG_DIR)/$*.json --links \
		--base-url http://127.0.0.1:$(FLOW_PORT)

serve-development:
	@$(call require_free_port,$(FLOW_PORT))
	$(call participant_server,development,$(FLOW_PORT),$(BACKUP_FLAG))

serve-rehearsal: preflight-rehearsal
	@$(call require_free_port,$(FLOW_PORT))
	$(call participant_server,rehearsal,$(FLOW_PORT),$(BACKUP_FLAG))

serve-study: preflight-study
	@$(call require_free_port,$(FLOW_PORT))
	$(call participant_server,study,$(FLOW_PORT),)

# Researcher previews: development configuration (its database; nothing is recorded for a preview), no backups,
# each on its own port so they can run beside each other and beside the participant flow.
review-simulated:
	@$(call require_free_port,$(REVIEW_SIMULATED_PORT))
	$(call participant_server,development,$(REVIEW_SIMULATED_PORT),--researcher-preview --corpus $(CORPUS_DIR)/simulated.json --no-backup)

review-study:
	@$(call require_free_port,$(REVIEW_STUDY_PORT))
	$(call participant_server,development,$(REVIEW_STUDY_PORT),--researcher-preview --corpus $(CORPUS_DIR)/study.json --no-backup)

review-validation:
	@$(call require_free_port,$(REVIEW_VALIDATION_PORT))
	$(call participant_server,development,$(REVIEW_VALIDATION_PORT),--researcher-preview --corpus $(CORPUS_DIR)/validation.json --no-backup)

# rehearsal_tunnel.py refuses non-simulated configurations, checks its port, and confirms the researcher routes
# answer 404 locally and through the tunnel before printing links.
tunnel:
	$(call preflight,rehearsal,$(TUNNEL_PORT))
	$(PY) $(SCRIPTS)/rehearsal_tunnel.py --port $(TUNNEL_PORT) $(BACKUP_FLAG)

# funnel_server.py starts the server without --researcher-preview, refuses an occupied port or an existing Tailscale
# share, checks the researcher routes answer 404 locally and at the public address, and never prints link tokens.
# For the study it also refuses while a consent, questionnaire or debriefing placeholder is left.
funnel-rehearsal: preflight-rehearsal
	@$(call require_free_port,$(FLOW_PORT))
	$(PY) $(SCRIPTS)/funnel_server.py --experiment-config $(CONFIG_DIR)/rehearsal.json --port $(FLOW_PORT)

funnel-study: preflight-study
	@$(call require_free_port,$(FLOW_PORT))
	$(PY) $(SCRIPTS)/funnel_server.py --experiment-config $(CONFIG_DIR)/study.json --port $(FLOW_PORT)
