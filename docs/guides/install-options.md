# Install options

How to install the `olf` command line with uv or pip, choose between the
toolchain `olf` manages for you and your own, and start a project from the demo
or from an empty skeleton. Everything here happens before `olf deploy`.

## Requirements

| You need | Check |
| --- | --- |
| A running Docker engine | `docker ps` succeeds |
| Python 3.12 or newer | `python3.12 --version`, unless you install with uv |

Git, uv, Make, Terraform, Helm, kubectl and kind are not host prerequisites.
`olf` provisions pinned Terraform, Helm, kubectl and kind itself, as described
under [Choose a toolchain mode](#choose-a-toolchain-mode).

## Install `olf`

Pick one. Both put an `olf` command on your `PATH`.

### With uv

```bash
uv tool install "openlakeforge==0.4.0a1" --python 3.12
```

uv fetches Python 3.12 for the tool if your machine has none.

### With pip

Install into a virtual environment, so `olf` does not mix with your system
Python:

```bash
python3.12 -m venv ~/.venvs/olf
source ~/.venvs/olf/bin/activate
python3.12 -m pip install "openlakeforge==0.4.0a1"
```

Run `source ~/.venvs/olf/bin/activate` again in each new shell before using
`olf`.

### Check the install

```bash
olf version
```

It prints the installed version, for example `0.4.0a1`.

## Choose a toolchain mode

`OLF_TOOLCHAIN_MODE` decides where `olf` finds Terraform, Helm, kubectl and
kind. Docker is always yours.

| Mode | What `olf` does | When to use it |
| --- | --- | --- |
| `managed` (default) | Downloads the versions pinned in the release, verifies them and keeps them under `~/.openlakeforge`. Never changes your `PATH` or your package manager. | Almost always. |
| `host` | Uses the Terraform, Helm, kubectl and kind found on your `PATH`. | You must use your own binaries, or the machine cannot download them. |

Any other value is rejected. To use your own binaries, export the variable in
every shell that runs `olf`:

```bash
export OLF_TOOLCHAIN_MODE=host
```

`OLF_HOME` moves the managed toolchain, the platform payload and `olf`'s state
away from `~/.openlakeforge`.

Inspect what is installed, or download everything in advance:

```bash
olf toolchain list
olf toolchain install
```

`olf doctor --provider local --phase foundation` also reports the active mode and where each tool
resolves, and fails when a tool is missing.

## Create a project

Run `olf init` in an empty directory. It checks Docker, provisions the
toolchain (in `managed` mode), then writes two things you own:

```bash
mkdir my-lakehouse && cd my-lakehouse
olf init
```

```text
my-lakehouse/
├── openlakeforge.yaml   # Deployment Profile: provider, stages, preset, access
└── lakehouse_code/      # sources, contracts, dbt projects, Dagster jobs
```

`lakehouse_code/` holds three demo data products. The profile is named after
the directory and enables one DEV stage on the Slim preset, with plain HTTP
and no sign-in. These options change it:

| Option | Effect |
| --- | --- |
| `--stages dev,prod` | Enable other stages: `dev`, `dev,prod` or `dev,uat,prod`. |
| `--preset full` | Enable Superset and OpenMetadata as well as the Slim data path. |
| `--name` | Name the profile something other than the directory. |
| `--profile-only` | Write only `openlakeforge.yaml`, into an existing project that has a `lakehouse_code/lakehouse.yaml`, such as one created by 0.2. |

`olf init` refuses to overwrite an existing `openlakeforge.yaml` or
`lakehouse_code/`.

## Start from an empty project

`--empty` skips the demo products and creates a skeleton with no source,
domain or product:

```bash
olf init --empty
```

It cannot deploy yet: `olf deploy` stops before it changes anything, saying the
project is not runnable, until the project holds its first source and product.
Create them:

```bash
olf source new crm --resource accounts
olf product new sales/accounts_report \
  --input crm/accounts --gold-table mart_accounts
```

`olf source new` writes the source and a placeholder CSV with one column;
`olf product new` creates the `sales` domain, its Silver table and the Gold
model from it. Then deploy as usual:

```bash
olf deploy --provider local
```
