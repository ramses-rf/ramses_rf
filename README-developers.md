![Linting](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-lint.yml/badge.svg)
![Typing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-type.yml/badge.svg)
![Testing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-test.yml/badge.svg)

# Ramses_rf developer's resource

## Installation

Confirm you have Python 3.13.x installed by running:
```
python3 --version
```

### Virtual environment

Create a `venv` virtual environment, for example on macOS or Linux:
```
mkdir /your-path-to/virtual-envs
mkdir /your-path-to/virtual-envs/ramses_rf
cd /your-path-to/virtual-envs/ramses_rf
Python3.13 -m venv ~/your-path-to/virtual-envs/ramses_rf
```
where `Python3.13` is the python version to set for the `venv`.

### Clone this repo

Clone this repo and install the requirements.
Using `pip`, in a location where your IDE has access:
```
git clone https://github.com/ramses-rf/ramses_rf
```

Activate the venv (repeat every new session):
```
cd /your-path-to/ramses_rf
source /your-path-to/virtual-envs/ramses_rf/bin/activate
```
and confirm your Terminal prompt looks like:
`(ramses_rf) user:ramses_rf`

### Install dependencies:
```
cd /your-path-to/ramses_rf
pip install -r requirements.txt
pip install -r requirements_dev.txt
```

Repeat this after a release update and also when dev_requirements change in master.

### Install pre-commit hook
Install the repo's pre-commit hook
```
pre-commit install
```

Running `pre-commit run` will only check staged files before a commit, while
`pre-commit run -a` will check all files.

## More
For more hints, see the [How to submit a PR wiki page](https://github.com/ramses-rf/ramses_rf/wiki/How-to-submit-a-PR)
