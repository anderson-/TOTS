HERMES ?= $(shell hermes config path 2>/dev/null | xargs -r dirname)
RUN_USER ?= $(shell id -un)
RUN_GROUP ?= $(shell id -gn)
SUDO ?= sudo

PY   := /usr/bin/python3
FONT := $(CURDIR)/assets/Unifont-APL8x16-18.psf.gz
ARGS ?=
EXE  = $(PY) $(CURDIR)/main.py $(ARGS)
CMDL = /usr/bin/openvt -s -w -- /bin/sh -c 'setfont $(FONT); exec runuser -u $(RUN_USER) -- $(EXE)'

define RELOAD
	@reload() { \
		printf 'Reload plugins now? (hermes gateway restart) [y/N] '; \
		read -r reply; \
		case "$$reply" in \
			[yY]*) hermes gateway restart ;; \
			*) echo 'Skipped. Run "hermes gateway restart" to reload.' ;; \
		esac; \
	}; reload
endef

.PHONY: check-hermes
check-hermes:
	@command -v hermes >/dev/null || { echo "Error: hermes not found"; exit 1; }
	@test -n "$(HERMES)" && test -d "$(HERMES)" || { \
		echo "Error: invalid HERMES directory: $(HERMES)"; exit 1; \
	}

.PHONY: install
install: update

.PHONY: update
update: check-hermes
	mkdir -p "$(HERMES)/plugins/stream-tap"
	ln -sfn "$(CURDIR)/plugin/__init__.py" "$(HERMES)/plugins/stream-tap/__init__.py"
	ln -sfn "$(CURDIR)/plugin/plugin.yaml" "$(HERMES)/plugins/stream-tap/plugin.yaml"
	hermes plugins enable stream-tap
	$(call RELOAD)

.PHONY: uninstall
uninstall: check-hermes
	hermes plugins disable stream-tap
	rm -rf "$(HERMES)/plugins/stream-tap"
	$(call RELOAD)

.PHONY: install-service
install-service:
	@command -v openvt >/dev/null || { echo "Error: openvt not found"; exit 1; }
	@command -v setfont >/dev/null || { echo "Error: setfont not found"; exit 1; }
	@{ \
		printf '%s\n' \
			'[Unit]' \
			'Description=TOTS Terminal Screensaver' \
			'After=systemd-user-sessions.service' \
			'' \
			'[Service]' \
			'Type=simple' \
			'WorkingDirectory=$(CURDIR)'; \
		printf 'ExecStart=%s\n' "$(CMDL)"; \
		printf '%s\n' \
			'StandardInput=tty' \
			'TTYPath=/dev/tty0' \
			'Restart=no' \
			'' \
			'[Install]' \
			'WantedBy=multi-user.target'; \
	} | $(SUDO) tee /etc/systemd/system/tots-screensaver.service >/dev/null
	$(SUDO) systemctl daemon-reload
	$(SUDO) systemctl enable --now tots-screensaver

.PHONY: uninstall-service
uninstall-service:
	$(SUDO) systemctl disable --now tots-screensaver
	$(SUDO) rm -f /etc/systemd/system/tots-screensaver.service
	$(SUDO) systemctl daemon-reload

.PHONY: run
run:
	$(PY) main.py $(ARGS)

.PHONY: render
render:
	time $(MAKE) run ARGS="--render-csv"

.PHONY: run-vt
run-vt:
	@printf 'Starting TOTS on a virtual terminal. Press q to exit.\n'
	$(SUDO) sh -c 'exec "$$@" </dev/tty0' sh $(CMDL)

.PHONY: stop
stop:
	kill $$(cat /tmp/hermes-stream.pid)

.PHONY: mock-stream
mock-stream:
	@printf 'Starting test stream. CTRL+c to exit.\n'
	$(PY) -m tools.feed $(ARGS)

.PHONY: suggest
suggest:
	time $(PY) -m tools.suggest

.PHONY: vhs
VHS_TAPES := $(filter-out assets/vhs/term.tape assets/vhs/themes.tape,$(wildcard assets/vhs/*.tape))
VHS_OUTPUTS := $(VHS_TAPES:.tape=.gif) $(VHS_TAPES:.tape=.mp4)

vhs: $(VHS_OUTPUTS)
	# export CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

assets/vhs/%.gif assets/vhs/%.mp4: assets/vhs/%.tape assets/vhs/term.tape
	time vhs $<
