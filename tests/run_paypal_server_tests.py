import sys, os, traceback, importlib.util
from pathlib import Path

# Add project root to sys.path
project_root = str(Path(__file__).resolve().parents[1])
sys.path.append(project_root)

# Load the test module
mod_path = os.path.join(project_root, 'tests', 'test_paypal_server.py')
spec = importlib.util.spec_from_file_location('test_mod', mod_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Gather test functions
functions = [getattr(mod, name) for name in dir(mod) if name.startswith('test_')]
passed = failed = 0
for fn in functions:
    try:
        fn()
        passed += 1
        print(f'PASS {fn.__name__}')
    except AssertionError as e:
        failed += 1
        print(f'FAIL {fn.__name__}: {e}')
    except Exception as e:
        failed += 1
        print(f'ERROR {fn.__name__}: {e}\n{traceback.format_exc()}')
print('---')
print(f'Total tests: {len(functions)}')
print(f'PASS: {passed}, FAIL: {failed}')
