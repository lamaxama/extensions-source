import gzip
import html
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from google.protobuf import json_format

import index_pb2

# Artifacts downloaded from the build jobs: one APK per extension plus the source metadata JSON
# emitted by each assembleRelease.
ARTIFACTS_DIR = Path.home() / "apk-artifacts"

# The checked-out `repo` branch we publish into (the working directory).
REPO_DIR = Path.cwd()
REPO_APK_DIR = REPO_DIR / "apk"
REPO_JAR_DIR = REPO_DIR / "jar"
REPO_APK_DIR.mkdir(parents=True, exist_ok=True)
REPO_JAR_DIR.mkdir(parents=True, exist_ok=True)

REPO_NAME = os.getenv("GITHUB_REPOSITORY", "lamaxama/extensions-source")
SOURCE_BRANCH = os.getenv("SOURCE_BRANCH", "main")
APK_BASE_URL = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@repo/apk"
JAR_BASE_URL = f"https://raw.githubusercontent.com/{REPO_NAME}/repo/jar"
ICON_BASE_URL = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{SOURCE_BRANCH}"

to_delete: list[str] = json.loads(sys.argv[1])
current_sha = sys.argv[2]
current_sha_short = current_sha[:7]

# Drop apks/icons for modules that were deleted or rebuilt (rebuilt ones are re-added below).
for module in to_delete:
    for file in REPO_APK_DIR.glob(f"tachiyomi-{module}-v*.*.*.apk"):
        print(f"removing {file.name}")
        file.unlink(missing_ok=True)
    for file in REPO_JAR_DIR.glob(f"tachiyomi-{module}-v*.*.*.jar"):
        print(f"removing {file.name}")
        file.unlink(missing_ok=True)

# Build index entries for the freshly built apks. Each extension's metadata comes from the
# source-info JSON emitted by its assembleRelease task (see GenerateSourceInfoTask); its APK is a
# sibling in the same build dir. aapt reads the icon out of the APK
new_extensions: list[tuple[index_pb2.Extension, Path, Path]] = []

SOURCE_DIR = Path(__file__).resolve().parents[2]
ICON_FILE = "res/mipmap-xhdpi/ic_launcher.png"


def get_icon_url(module: str, theme: str | None) -> str:
    module_icon = f"src/{module.replace('.', '/')}/{ICON_FILE}"
    if (SOURCE_DIR / module_icon).exists():
        return f"{ICON_BASE_URL}/{module_icon}"

    if theme:
        theme_icon = f"lib-multisrc/{theme}/{ICON_FILE}"
        if (SOURCE_DIR / theme_icon).exists():
            return f"{ICON_BASE_URL}/{theme_icon}"

    return f"{ICON_BASE_URL}/core/src/main/{ICON_FILE}"

for info_file in ARTIFACTS_DIR.glob("**/keiyoushi-source-info.json"):
    with info_file.open(encoding="utf-8") as f:
        info = json.load(f)
    package_name = info["packageName"]
    apk = next((info_file.parent / "outputs/apk/release").glob("*.apk"), None)
    if apk is None:
        raise FileNotFoundError(
            f"{package_name}: no release apk found under {info_file.parent}"
        )

    jar = next((info_file.parent / "outputs/jar/release").glob("*.jar"), None)
    if jar is None:
        raise FileNotFoundError(
            f"{package_name}: no release jar found under {info_file.parent}"
        )

    (REPO_APK_DIR / apk.name).write_bytes(apk.read_bytes())
    (REPO_JAR_DIR / jar.name).write_bytes(jar.read_bytes())

    ext = index_pb2.Extension(
        name=info["name"],
        packageName=package_name,
        resources=index_pb2.Resources(
            apkUrl=f"{APK_BASE_URL}/{apk.name}",
            jarUrl=f"{JAR_BASE_URL}/{jar.name}",
            iconUrl=get_icon_url(info["module"], info.get("theme")),
        ),
        extensionLib=info["extensionLib"],
        versionCode=info["versionCode"],
        versionName=info["versionName"],
        contentWarning=info["contentWarning"],
        sources=[
            index_pb2.Source(
                id=int(source["id"]),
                name=source["name"],
                language=source["lang"],
                homeUrl=source["baseUrl"],
                mirrorUrls=source.get("mirrorUrls", []),
            )
            for source in info["sources"]
        ],
    )
    new_extensions.append((ext, apk, jar))

# Merge with the already-published index, dropping the deleted/rebuilt modules.
index_path = REPO_DIR / "index.json"
if index_path.exists():
    with index_path.open() as f:
        remote_proto = json_format.Parse(f.read(), index_pb2.Index())
else:
    remote_proto = index_pb2.Index()

all_extensions = [
    ext
    for ext in remote_proto.extensionList.extensions
    if not any(ext.packageName.endswith(f".{module}") for module in to_delete)
]
all_extensions.extend([i[0] for i in new_extensions])
all_extensions.sort(key=lambda ext: ext.packageName)

index = index_pb2.Index(
    name="Lamaxama Extensions",
    badgeLabel="LAMA",
    signingKey="f429a1df512c483ccf63e26908af0402b17d809267091d3257b1532c6f293e24",
    contact=index_pb2.Contact(
        website="https://github.com/lamaxama/extensions-source",
    ),
    extensionList=index_pb2.ExtensionList(extensions=all_extensions),
)

with REPO_DIR.joinpath("index.json").open("w", encoding="utf-8") as f:
    f.write(
        json_format.MessageToJson(
            index,
            always_print_fields_with_no_presence=False,
            preserving_proto_field_name=True,
        )
    )

with REPO_DIR.joinpath("index.pb").open("wb") as f:
    f.write(gzip.compress(index.SerializeToString(deterministic=True)))

with REPO_DIR.joinpath("index.html").open("w", encoding="utf-8") as f:
    f.write(
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<title>apks</title>\n</head>\n<body>\n<pre>\n'
    )
    for ext in all_extensions:
        apk_escaped = html.escape(ext.resources.apkUrl)
        name_escaped = html.escape(f"Tachiyomi: {ext.name}")
        f.write(f'<a href="{apk_escaped}">{name_escaped}</a>\n')
    f.write("</pre>\n</body>\n</html>\n")

# --- Upload assets as release ---
if not new_extensions:
    sys.exit(0)

ASSET_LIMIT = 495  # Actual limit is 1000, but we upload two assets per extension.
total_extensions = len(new_extensions)
release_count = math.ceil(total_extensions / ASSET_LIMIT)
ext_per_release = math.ceil(total_extensions / release_count)


def run_gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()

    print(f"gh {' '.join(args)} failed: {result.stderr}")
    sys.exit(result.returncode)


def ensure_release(tag: str):
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO_NAME],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        print(f"Using existing release {tag}")
        return

    print(f"Creating release {tag}")
    run_gh(
        "release",
        "create",
        tag,
        "--repo",
        REPO_NAME,
        "--title",
        f"Repository Update {tag}",
        "--notes",
        f"Automated update from {REPO_NAME}@{current_sha}",
    )


def upload_assets(tag: str, files: list[Path]):
    if not files:
        return
    print(f"Uploading {len(files)} assets to {tag}")
    run_gh(
        "release",
        "upload",
        tag,
        *[str(f) for f in files],
        "--repo",
        REPO_NAME,
        "--clobber",
    )


def get_release_tag(c_index: int) -> str:
    return f"{current_sha_short}-{c_index}" if release_count > 1 else current_sha_short


for i in range(0, total_extensions, ext_per_release):
    batch = new_extensions[i:i + ext_per_release]
    tag = get_release_tag(i // ext_per_release)
    ensure_release(tag)

    files_to_upload = []
    for ext, apk, jar in batch:
        files_to_upload.extend([apk, jar])

    upload_assets(tag, files_to_upload)
