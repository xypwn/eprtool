import argparse
import glob
import os
import re


def collect_isort_mypyc_bins() -> list[tuple[str, str]]:
    res = []
    import isort
    pkg_dir = os.path.dirname(os.path.dirname(isort.__file__))
    for b in glob.glob(os.path.join(pkg_dir, "*__mypyc*")):
        if b.endswith((".pyd", ".so")):
            res.append((b, "."))
    return res

def path_re_norm(s: str) -> str:
    return s.replace("/", "\\\\") if os.name == "nt" else s

def parse_args():
    argp = argparse.ArgumentParser()
    argp.add_argument("--guionly", action="store_true")
    return argp.parse_args()

def exclude_datas(datas: list[tuple[str, str, str]], exclude_regexes: list[str]) -> list[tuple[str, str, str]]:
    exclude = [re.compile(path_re_norm(p)) for p in exclude_regexes]
    return [(dst, src, typ) for dst, src, typ in datas
        if not any(p.match(src) is not None for p in exclude)]

DATAS_SIMPLEMMA_NON_EN_DE = [
    # Filter out all simplemma language files that aren't German or English (saves ~20MiB)
    r".*/simplemma/strategies/dictionaries/data/(?!(en|de)\.)\w+\.plzma$",
    # We don't use the default imgui_bundle resources
    r".*/imgui_bundle/(assets|demos_assets|demos_cpp|demos_python)/.*$",
]