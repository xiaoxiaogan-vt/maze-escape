import subprocess
import json
import re
import stat
import tempfile
from pathlib import Path
import shutil
import os
import time

# In GitHub Actions these come from the workflow (env:). Locally, export them yourself.
REPO_URL = os.environ["REPO_URL"]            # e.g. https://github.com/owner/repo
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

AZ = shutil.which("az") or "az"

# =====================================================
# EDIT THESE CONFIGS ONLY
# =====================================================

# Empty / unset = auto-detect default branch
BASELINE_BRANCH = os.environ.get("BASELINE_BRANCH") or None
CANDIDATE_BRANCH = os.environ.get("CANDIDATE_BRANCH") or None

PROJECT_NAME = os.environ.get("PROJECT_NAME", "behavior-test-demo")

RESOURCE_GROUP = os.environ.get("RESOURCE_GROUP", "behavior-testing-rg")
LOCATION = os.environ.get("LOCATION", "eastus2")

# must be globally unique, lowercase letters/numbers only
ACR_NAME = os.environ.get("ACR_NAME", "behaviortestacr12345")
CONTAINER_ENV = os.environ.get("CONTAINER_ENV", "behavior-testing-env")

DOCKERFILE_PATH = "Dockerfile"
TARGET_PORT = None  # None = auto-detect per project type

# Set this to the subfolder containing index.html (e.g. "visualization", "public/app").
# Leave as None to auto-detect from the repo.
STATIC_SERVE_DIR = None

# If True, rebuild images and update apps even if they already exist.
FORCE_REDEPLOY = True

# =====================================================
# SCRIPT LOGIC
# =====================================================

SECRET_FLAGS = {"--registry-password"}


def mask(cmd):
    out, hide = [], False
    for item in cmd:
        out.append("***" if hide or "x-access-token:" in item else item)
        hide = item in SECRET_FLAGS
    return " ".join(out)


def run(cmd, cwd=None, allow_fail=False):
    print("+", mask(cmd), flush=True)

    result = subprocess.run(cmd, cwd=cwd, text=True)

    if result.returncode != 0 and not allow_fail:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {mask(cmd)}")

    return result.returncode


def output(cmd):
    print("\n+", mask(cmd), flush=True)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {mask(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def force_remove(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def exists(cmd):
    return run(cmd, allow_fail=True) == 0


def safe_name(text):
    return (
        text.lower()
        .replace("/", "-")
        .replace("_", "-")
        .replace(".", "-")
    )


def ensure_azure_ready():
    run([AZ, "account", "show"])

    run([
        AZ, "extension", "add",
        "--name", "containerapp",
        "--upgrade"
    ], allow_fail=True)

    run([AZ, "provider", "register", "--namespace", "Microsoft.App"], allow_fail=True)
    run([AZ, "provider", "register", "--namespace", "Microsoft.OperationalInsights"], allow_fail=True)
    run([AZ, "provider", "register", "--namespace", "Microsoft.ContainerRegistry"], allow_fail=True)


def ensure_resource_group():
    run([
        AZ, "group", "create",
        "--name", RESOURCE_GROUP,
        "--location", LOCATION
    ])

def ensure_acr():
    if exists([AZ, "acr", "show", "--name", ACR_NAME, "--resource-group", RESOURCE_GROUP]):
        print(f"ACR already exists: {ACR_NAME}")
    else:
        run([
            AZ, "acr", "create",
            "--name", ACR_NAME,
            "--resource-group", RESOURCE_GROUP,
            "--sku", "Basic",
            "--admin-enabled", "true"
        ])

def ensure_container_env():
    if exists([
        AZ, "containerapp", "env", "show",
        "--name", CONTAINER_ENV,
        "--resource-group", RESOURCE_GROUP
    ]):
        print(f"Container App environment already exists: {CONTAINER_ENV}")
    else:
        run([
            AZ, "containerapp", "env", "create",
            "--name", CONTAINER_ENV,
            "--resource-group", RESOURCE_GROUP,
            "--location", LOCATION
        ])

def detect_default_branch(repo_url):
    """Ask the remote which branch HEAD points to without cloning."""
    raw = output(["git", "ls-remote", "--symref", repo_url_with_token(repo_url), "HEAD"])
    for line in raw.splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("refs/heads/")[1].split("\t")[0]
    return "main"


def repo_url_with_token(repo_url):
    return repo_url.replace(
        "https://github.com/",
        f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
    )

def clone_branch(repo_url, branch, target_dir):
    clone_url = repo_url_with_token(repo_url)

    run([
        "git", "clone",
        "--depth", "1",
        "--branch", branch,
        "--single-branch",
        clone_url,
        str(target_dir)
    ])

    sha = output(["git", "-C", str(target_dir), "rev-parse", "--short", "HEAD"])

    # .git/config contains the token URL — remove it before "COPY . ." can bake it into the image
    shutil.rmtree(target_dir / ".git", onexc=force_remove)

    return sha

def build_image(branch, sha, repo_dir):
    tag = f"{safe_name(branch)}-{sha}-{int(time.time())}"
    image_name = f"{PROJECT_NAME}:{tag}"

    login_server = output([
        AZ, "acr", "show",
        "--name", ACR_NAME,
        "--query", "loginServer",
        "-o", "tsv"
    ])

    full_image = f"{login_server}/{image_name}"

    # ACR Tasks are not permitted on this subscription — build locally and push
    run(["docker", "build",
         "--platform", "linux/amd64",
         "-f", str(repo_dir / DOCKERFILE_PATH),
         "-t", full_image,
         str(repo_dir)])

    run([AZ, "acr", "login", "--name", ACR_NAME])
    run(["docker", "push", full_image])

    return full_image

def wait_for_revision_healthy(app_name, timeout=180, interval=5):
    """Poll the container app's latest revision until it reports healthy/running.

    az containerapp update/create returns as soon as the revision resource is
    created, not once it's actually serving traffic — a crashing container
    otherwise looks like a successful deploy while the old revision keeps
    serving (or the new one sits at 0 replicas eating all traffic weight).
    """
    deadline = time.time() + timeout
    last_state = None

    while time.time() < deadline:
        revisions = json.loads(output([
            AZ, "containerapp", "revision", "list",
            "--name", app_name,
            "--resource-group", RESOURCE_GROUP,
            "-o", "json"
        ]))

        if revisions:
            latest = max(revisions, key=lambda r: r["properties"]["createdTime"])
            props = latest["properties"]
            last_state = (latest["name"], props["healthState"], props["runningState"], props.get("runningStateDetails"))
            if props["healthState"] == "Healthy" and props["runningState"] == "Running":
                return

        time.sleep(interval)

    name, health, running, details = last_state or ("unknown", "unknown", "unknown", "unknown")
    raise RuntimeError(
        f"Container app '{app_name}' revision '{name}' did not become healthy within {timeout}s "
        f"(healthState={health}, runningState={running}, details={details})"
    )


def create_or_update_container_app(app_name, image, port):
    app_exists = exists([
        AZ, "containerapp", "show",
        "--name", app_name,
        "--resource-group", RESOURCE_GROUP
    ])

    if app_exists:
        # "containerapp update" has no --target-port flag, and the target
        # port is baked into a revision at creation time (not read live from
        # ingress config) — so the ingress port must be corrected *before*
        # the image update creates the new revision, or the new revision
        # still boots with the stale port and fails its health check.
        run([
            AZ, "containerapp", "ingress", "update",
            "--name", app_name,
            "--resource-group", RESOURCE_GROUP,
            "--target-port", str(port)
        ])
        run([
            AZ, "containerapp", "update",
            "--name", app_name,
            "--resource-group", RESOURCE_GROUP,
            "--image", image
        ])
    else:
        acr_username = output([
            AZ, "acr", "credential", "show",
            "--name", ACR_NAME,
            "--query", "username",
            "-o", "tsv"
        ])

        acr_password = output([
            AZ, "acr", "credential", "show",
            "--name", ACR_NAME,
            "--query", "passwords[0].value",
            "-o", "tsv"
        ])

        login_server = output([
            AZ, "acr", "show",
            "--name", ACR_NAME,
            "--query", "loginServer",
            "-o", "tsv"
        ])

        run([
            AZ, "containerapp", "create",
            "--name", app_name,
            "--resource-group", RESOURCE_GROUP,
            "--environment", CONTAINER_ENV,
            "--image", image,
            "--target-port", str(port),
            "--ingress", "external",
            "--registry-server", login_server,
            "--registry-username", acr_username,
            "--registry-password", acr_password
        ])

    wait_for_revision_healthy(app_name)

    url = output([
        AZ, "containerapp", "show",
        "--name", app_name,
        "--resource-group", RESOURCE_GROUP,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv"
    ])

    return f"https://{url}"


# =====================================================
# AUTO-DOCKERFILE GENERATION
# =====================================================

FRONTEND_SUBDIR_NAMES = ["frontend", "client", "web", "ui"]
BACKEND_SUBDIR_NAMES  = ["backend", "server", "api", "app"]

def detect_fullstack_subdirs(repo_dir):
    """Detect frontend/backend subdir layout. Returns (frontend_dir, backend_dir, backend_is_root)."""
    frontend_dir = next(
        (repo_dir / name for name in FRONTEND_SUBDIR_NAMES
         if (repo_dir / name / "package.json").exists() or (repo_dir / name / "index.html").exists()),
        None
    )
    backend_dir = next(
        (repo_dir / name for name in BACKEND_SUBDIR_NAMES
         if (repo_dir / name / "package.json").exists()),
        None
    )
    if frontend_dir and backend_dir:
        return frontend_dir, backend_dir, False
    # Frontend in subdir, backend at repo root
    if frontend_dir and (repo_dir / "package.json").exists():
        return frontend_dir, repo_dir, True
    return None, None, False


def detect_project_type(repo_dir):
    """Detect project type from files in the repo."""

    # PHP / Symfony projects — check first: composer.json means this is
    # fundamentally a PHP app even if it also ships a package.json for
    # building frontend assets (webpack/storybook/etc.)
    if (repo_dir / "composer.json").exists():
        try:
            composer = json.loads((repo_dir / "composer.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            composer = {}

        composer_deps = {**composer.get("require", {}), **composer.get("require-dev", {})}

        if "symfony/framework-bundle" in composer_deps or (repo_dir / "public" / "index.php").exists():
            return "symfony", {}

        if (repo_dir / "index.php").exists():
            return "php", {}

    # Check for separate frontend + backend subdirectories first
    frontend_dir, backend_dir, backend_is_root = detect_fullstack_subdirs(repo_dir)
    if frontend_dir and backend_dir and not backend_is_root:
        return "fullstack-subdirs", {
            "__frontend_dir": frontend_dir.name,
            "__backend_dir": backend_dir.name,
        }
    if frontend_dir and backend_dir and backend_is_root:
        return "fullstack-fe-subdir", {
            "__frontend_dir": frontend_dir.name,
        }

    package_json_path = repo_dir / "package.json"

    if package_json_path.exists():
        try:
            pkg = json.loads(package_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "node-unknown", {}

        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {})

        # Next.js
        if "next" in deps:
            return "nextjs", scripts

        # Nuxt / Vue SSR
        if "nuxt" in deps:
            return "nuxt", scripts

        # Vue (Vite or CLI)
        if "vue" in deps:
            return "vue", scripts

        # Angular
        if "@angular/core" in deps:
            return "angular", scripts

        # Vite (generic)
        if "vite" in deps:
            return "vite", scripts

        # Express / backend-only Node
        if "express" in deps:
            return "express", scripts

        # Has a build script → assume static React/frontend
        if "build" in scripts:
            return "node-static", scripts

        # Has a start script → assume server
        if "start" in scripts:
            return "node-server", scripts

        # Has dev/serve/preview but no build/start → dev server
        if any(s in scripts for s in ["dev", "serve", "preview"]):
            return "node-dev", scripts

        # Pure static frontend — has index.html, just needs serving
        # Common for D3, vanilla JS, p5.js, three.js projects
        if STATIC_SERVE_DIR:
            return "static-html", {**scripts, "__serve_dir": STATIC_SERVE_DIR}

        for html_location in ["index.html", "public/index.html", "src/index.html", "visualization/index.html"]:
            if (repo_dir / html_location).exists():
                serve_dir = str(Path(html_location).parent) if "/" in html_location else "."
                return "static-html", {**scripts, "__serve_dir": serve_dir}

        # Has a main entry point → simple node app
        # (only trust package.json's declared entry if the file actually exists —
        # "main" often defaults to "index.js" even in non-runnable packages)
        main_entry = pkg.get("main") or pkg.get("module")
        if main_entry and (repo_dir / main_entry).exists():
            return "node-entry", {**scripts, "__main_entry": main_entry}

        # Check for common entry files on disk
        for entry_file in ["index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts", "src/index.js", "src/index.ts"]:
            if (repo_dir / entry_file).exists():
                return "node-entry", {**scripts, "__main_entry": entry_file}

        # Last resort — still a node project, just try npm install + start
        return "node-unknown", scripts

    # Python projects
    if (repo_dir / "requirements.txt").exists() or (repo_dir / "pyproject.toml").exists():
        if (repo_dir / "manage.py").exists():
            return "django", {}
        if (repo_dir / "app.py").exists() or (repo_dir / "wsgi.py").exists():
            return "flask", {}

        # Check for common Python entry files
        for entry_file in ["main.py", "run.py", "server.py"]:
            if (repo_dir / entry_file).exists():
                return "python-entry", {"__main_entry": entry_file}

        return "python-unknown", {}

    # Pure static HTML — no package.json, no Python, just HTML/CSS/JS
    if STATIC_SERVE_DIR:
        return "static-html", {"__serve_dir": STATIC_SERVE_DIR}

    for html_location in ["index.html", "public/index.html", "src/index.html", "visualization/index.html"]:
        if (repo_dir / html_location).exists():
            serve_dir = str(Path(html_location).parent) if "/" in html_location else "."
            return "static-html", {"__serve_dir": serve_dir}

    return "unknown", {}


def detect_build_output_dir(repo_dir, project_type):
    """Detect the build output directory based on project type and config files."""

    # Vite config check — vite can customize output dir
    for vite_config in ["vite.config.ts", "vite.config.js"]:
        config_path = repo_dir / vite_config
        if config_path.exists():
            content = config_path.read_text(encoding="utf-8", errors="ignore")
            if "outDir" in content:
                # Common custom output dirs
                for candidate in ["build", "output", "public"]:
                    if candidate in content:
                        return candidate

    # Default output dirs by project type
    defaults = {
        "nextjs": ".next",
        "nuxt": ".output",
        "vue": "dist",
        "angular": "dist",
        "vite": "dist",
        "node-static": "build",
    }

    guessed = defaults.get(project_type, "build")

    return guessed


def write_fullstack_configs(repo_dir, port):
    """Write nginx and supervisord config files into the repo so the Dockerfile can COPY them."""
    (repo_dir / ".docker-supervisord.conf").write_text(
        "[supervisord]\n"
        "nodaemon=true\n"
        "\n"
        "[program:backend]\n"
        "command=npm start\n"
        "directory=/backend\n"
        "environment=PORT=4000\n"
        "autorestart=true\n"
        "stdout_logfile=/dev/stdout\n"
        "stdout_logfile_maxbytes=0\n"
        "stderr_logfile=/dev/stderr\n"
        "stderr_logfile_maxbytes=0\n"
        "\n"
        "[program:nginx]\n"
        'command=nginx -g "daemon off;"\n'
        "autorestart=true\n"
        "stdout_logfile=/dev/stdout\n"
        "stdout_logfile_maxbytes=0\n"
        "stderr_logfile=/dev/stderr\n"
        "stderr_logfile_maxbytes=0\n",
        encoding="utf-8",
    )
    (repo_dir / ".docker-nginx.conf").write_text(
        "server {\n"
        f"    listen {port};\n"
        "    location / {\n"
        "        root /usr/share/nginx/html;\n"
        "        try_files $uri $uri/ /index.html;\n"
        "    }\n"
        "    location /api/ {\n"
        "        proxy_pass http://localhost:4000/;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


DEFAULT_PORTS = {
    "django": 8000,
    "flask": 5000,
    "python-entry": 5000,
    "python-unknown": 5000,
    "symfony": 8000,
    "php": 8000,
}

def generate_dockerfile(repo_dir, project_type, scripts):
    """Generate a Dockerfile based on detected project type."""

    port = TARGET_PORT or DEFAULT_PORTS.get(project_type, 3000)

    dockerfiles = {
        "nextjs": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
EXPOSE {port}
CMD ["npm", "start"]
""",

        "nuxt": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
EXPOSE {port}
CMD ["node", ".output/server/index.mjs"]
""",

        "vue": f"""FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
FROM node:20-alpine
RUN npm install -g serve
COPY --from=build /app/{detect_build_output_dir(repo_dir, "vue")} /app
EXPOSE {port}
CMD ["serve", "-s", "/app", "-l", "{port}"]
""",

        "angular": f"""FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
FROM node:20-alpine
RUN npm install -g serve
COPY --from=build /app/{detect_build_output_dir(repo_dir, "angular")} /app
EXPOSE {port}
CMD ["serve", "-s", "/app", "-l", "{port}"]
""",

        "vite": f"""FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
FROM node:20-alpine
RUN npm install -g serve
COPY --from=build /app/{detect_build_output_dir(repo_dir, "vite")} /app
EXPOSE {port}
CMD ["serve", "-s", "/app", "-l", "{port}"]
""",

        "express": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
{"RUN npm run build" if "build" in scripts else ""}
EXPOSE {port}
CMD ["npm", "start"]
""",

        "node-static": f"""FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
RUN npm run build
FROM node:20-alpine
RUN npm install -g serve
COPY --from=build /app/{detect_build_output_dir(repo_dir, "node-static")} /app
EXPOSE {port}
CMD ["serve", "-s", "/app", "-l", "{port}"]
""",

        "node-server": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
{"RUN npm run build" if "build" in scripts else ""}
EXPOSE {port}
CMD ["npm", "start"]
""",

        "django": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD ["python", "manage.py", "runserver", "0.0.0.0:{port}"]
""",

        "flask": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
EXPOSE {port}
CMD ["gunicorn", "-b", "0.0.0.0:{port}", "app:app"]
""",

        "node-dev": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD {json.dumps(["npm", "run", next(s for s in ["dev", "serve", "preview"] if s in scripts)])}
""" if any(s in scripts for s in ["dev", "serve", "preview"]) else None,

        "node-entry": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
{"RUN npm run build" if "build" in scripts else ""}
EXPOSE {port}
ENV PORT={port}
CMD ["node", "{scripts.get('__main_entry', 'index.js')}"]
""",

        "node-unknown": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN npm install --legacy-peer-deps || npm install
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["node", "index.js"]
""",

        "python-entry": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* pyproject.toml* ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE {port}
CMD ["python", "{scripts.get('__main_entry', 'main.py')}"]
""",

        "python-unknown": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* pyproject.toml* ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE {port}
CMD ["python", "main.py"]
""",

        "symfony": f"""FROM php:8.3-cli
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unzip git libicu-dev libzip-dev \\
    && docker-php-ext-install intl pdo pdo_mysql zip \\
    && curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer \\
    && rm -rf /var/lib/apt/lists/*
COPY composer.json composer.lock* ./
RUN composer install --no-dev --no-scripts --no-interaction --ignore-platform-reqs
COPY . .
RUN composer dump-autoload --optimize --no-scripts || true
ENV APP_ENV=prod
EXPOSE {port}
CMD ["php", "-S", "0.0.0.0:{port}", "-t", "public"]
""",

        "php": f"""FROM php:8.3-cli
WORKDIR /app
COPY . .
EXPOSE {port}
CMD ["php", "-S", "0.0.0.0:{port}"]
""",

        "static-html": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json yarn.lock* ./
RUN if [ -f package.json ]; then npm install --legacy-peer-deps || npm install; fi
COPY . .
RUN npm install -g serve
EXPOSE {port}
CMD ["serve", "-s", "{scripts.get('__serve_dir', '.')}", "-l", "{port}"]
""",

    }

    if project_type == "fullstack-fe-subdir":
        fe = scripts['__frontend_dir']
        write_fullstack_configs(repo_dir, port)
        return f"""FROM node:20-alpine AS frontend-build
WORKDIR /app
COPY {fe}/package*.json ./
RUN npm install --legacy-peer-deps || npm install
COPY {fe}/ .
RUN npm run build 2>/dev/null || npm run generate 2>/dev/null || true
RUN mkdir -p /app/output && (cp -r /app/build/. /app/output/ 2>/dev/null || true) && (cp -r /app/dist/. /app/output/ 2>/dev/null || true)

FROM node:20-alpine AS backend-build
WORKDIR /app
COPY package*.json ./
RUN npm install --legacy-peer-deps || npm install
COPY . .

FROM node:20-alpine
RUN apk add --no-cache nginx supervisor
COPY --from=frontend-build /app/output /usr/share/nginx/html
COPY --from=backend-build /app /backend
COPY .docker-supervisord.conf /etc/supervisord.conf
COPY .docker-nginx.conf /etc/nginx/http.d/default.conf
EXPOSE {port}
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
"""

    if project_type == "fullstack-subdirs":
        fe = scripts['__frontend_dir']
        be = scripts['__backend_dir']
        write_fullstack_configs(repo_dir, port)
        return f"""FROM node:20-alpine AS frontend-build
WORKDIR /app
COPY {fe}/package*.json ./
RUN npm install --legacy-peer-deps || npm install
COPY {fe}/ .
RUN npm run build 2>/dev/null || npm run generate 2>/dev/null || true
RUN mkdir -p /app/output && (cp -r /app/build/. /app/output/ 2>/dev/null || true) && (cp -r /app/dist/. /app/output/ 2>/dev/null || true)

FROM node:20-alpine AS backend-build
WORKDIR /app
COPY {be}/package*.json ./
RUN npm install --legacy-peer-deps || npm install
COPY {be}/ .

FROM node:20-alpine
RUN apk add --no-cache nginx supervisor
COPY --from=frontend-build /app/output /usr/share/nginx/html
COPY --from=backend-build /app /backend
COPY .docker-supervisord.conf /etc/supervisord.conf
COPY .docker-nginx.conf /etc/nginx/http.d/default.conf
EXPOSE {port}
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
"""

    return dockerfiles.get(project_type)


def resolve_port(project_type):
    return TARGET_PORT or DEFAULT_PORTS.get(project_type, 3000)


def ensure_dockerfile(repo_dir):
    """Check for existing Dockerfile; auto-generate if missing. Returns resolved port."""

    dockerfile = repo_dir / "Dockerfile"

    if dockerfile.exists():
        print("Dockerfile found — using existing.")
        m = re.search(r"^\s*EXPOSE\s+(\d+)", dockerfile.read_text(encoding="utf-8", errors="ignore"), re.M | re.I)
        return TARGET_PORT or (int(m.group(1)) if m else 3000)

    project_type, scripts = detect_project_type(repo_dir)
    print(f"No Dockerfile found. Detected project type: {project_type}")

    content = generate_dockerfile(repo_dir, project_type, scripts)

    if content is None:
        # List files for debugging
        top_files = [f.name for f in repo_dir.iterdir() if f.is_file()][:20]
        raise RuntimeError(
            f"Cannot auto-generate Dockerfile for project type '{project_type}'.\n"
            f"Files found in repo root: {top_files}\n"
            f"Please add a Dockerfile manually to the repo."
        )

    dockerfile.write_text(content, encoding="utf-8")
    # Verify file was actually written
    if not dockerfile.exists():
        raise RuntimeError("Dockerfile was written but cannot be found on disk!")

    # Fix .dockerignore — if it exists, make sure Dockerfile is NOT excluded
    dockerignore = repo_dir / ".dockerignore"
    if dockerignore.exists():
        ignore_content = dockerignore.read_text(encoding="utf-8", errors="ignore")

        if "Dockerfile" in ignore_content or "*" in ignore_content.splitlines():
            print("WARNING: .dockerignore may exclude Dockerfile — adding exception")
            with open(dockerignore, "a", encoding="utf-8") as f:
                f.write("\n!Dockerfile\n")

    # Log for reproducibility
    print(f"Auto-generated Dockerfile for [{project_type}]:")
    print("--- Dockerfile start ---")
    print(content)
    print("--- Dockerfile end ---")

    return resolve_port(project_type)


def deploy_branch(label, branch):
    app_name = safe_name(f"{PROJECT_NAME}-{label}")

    print(f"\n========== Deploying {label}: {branch} ==========")

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp) / "repo"

        sha = clone_branch(REPO_URL, branch, repo_dir)
        port = ensure_dockerfile(repo_dir)

        image = build_image(branch, sha, repo_dir)
        url = create_or_update_container_app(app_name, image, port)

    return url, sha

def main():
    ensure_azure_ready()
    ensure_resource_group()
    ensure_acr()
    ensure_container_env()

    default_branch = detect_default_branch(REPO_URL)
    print(f"Default branch detected: {default_branch}")

    baseline_branch = BASELINE_BRANCH or default_branch
    candidate_branch = CANDIDATE_BRANCH or default_branch

    baseline_url, baseline_sha = deploy_branch("baseline", baseline_branch)
    candidate_url, candidate_sha = deploy_branch("candidate", candidate_branch)

    print("\n========== Deployment Complete ==========")
    print(f"Baseline branch:  {baseline_branch} ({baseline_sha})")
    print(f"Baseline URL:     {baseline_url}")
    print(f"Candidate branch: {candidate_branch} ({candidate_sha})")
    print(f"Candidate URL:    {candidate_url}")

    # Expose the URLs to later workflow steps / jobs (e.g. your behavior tests)
    if gh_out := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"baseline_url={baseline_url}\n")
            f.write(f"candidate_url={candidate_url}\n")

    if gh_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write("## Behavior-test deployments\n\n")
            f.write("| | Branch | Commit | URL |\n|---|---|---|---|\n")
            f.write(f"| Baseline | `{baseline_branch}` | `{baseline_sha}` | {baseline_url} |\n")
            f.write(f"| Candidate | `{candidate_branch}` | `{candidate_sha}` | {candidate_url} |\n")


if __name__ == "__main__":
    main()
