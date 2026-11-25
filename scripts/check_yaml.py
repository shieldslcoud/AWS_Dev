#!/usr/bin/env python3
"""Fail if any tracked YAML file does not parse. Stdlib + PyYAML (already installed with cfn-lint).

CloudFormation templates use short-form tags (!Ref, !Sub, ...), so unknown tags are accepted.
"""
import subprocess, sys
import yaml

yaml.SafeLoader.add_multi_constructor("!", lambda loader, suffix, node: None)

files = subprocess.run(["git", "ls-files", "*.yml", "*.yaml"], stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.split()
bad = 0
for f in files:
    try:
        with open(f) as fh:
            list(yaml.safe_load_all(fh))
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"YAML error in {f}: {str(e).splitlines()[0]}")
print(f"checked {len(files)} YAML files, {bad} invalid")
sys.exit(1 if bad else 0)
