# Image of the job-match pipeline (goal.md 5.9a, Phase 5). Build: docker compose build
# What is NOT in the image (see .dockerignore and compose.yaml): config.yaml, .env, data/, exchange/, output/, backups/, logs/.
# They are mounted from the host when the container starts.
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

# TZ: the container clock shows Zurich time (the scheduler uses the 'timezone' setting of config.yaml either way).
# tzdata (system package) is needed for TZ; the Python package 'tzdata' in requirements.txt serves zoneinfo.
ENV TZ=Europe/Zurich \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RECRUITMENT_CONTAINER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

# The Claude Code CLI for the llm service (v0.78.0, goal.md 5.9a, 5.10): the stable release 2.1.311 as one native binary, checked
# against the sha256 of the release manifest (https://downloads.claude.ai/claude-code-releases/2.1.311/manifest.json, linux-x64).
# No Node. The updater is off: a new version is a deliberate bump of this line, of headless.CLI_VERSION and of the checksum.
ADD --checksum=sha256:86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada --chmod=755 https://downloads.claude.ai/claude-code-releases/2.1.311/linux-x64/claude /usr/local/bin/claude
ENV DISABLE_AUTOUPDATER=1     DISABLE_UPDATES=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Code, fonts (app/fonts), prompts, the master CV and the default configuration. Nothing private.
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY profile/ ./profile/
COPY config.example.yaml ./
# The three role agents of the headless chain (the worker builds the --agents file from them; .dockerignore lets only these through).
COPY .claude/agents/scorer-cv.md .claude/agents/letter-writer.md .claude/agents/doc-reviewer.md ./headless/agents/

# Empty mount points owned by the app user; compose mounts the host folders over them.
RUN mkdir -p data exchange output backups logs /home/llm \
    && chown -R app:app /app/data /app/exchange /app/output /app/backups /app/logs /home/llm

USER app

# The scheduler is off unless schedule.enabled is true in config.yaml; then it only idles, so
# 'docker compose exec recruitment python -m app finish' always works.
CMD ["python", "-m", "app", "schedule"]
