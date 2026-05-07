import os

# Map: package -> set of module names in that package
pkg_modules = {}
for root, dirs, files in os.walk('src'):
    pkg = os.path.relpath(root, 'src').replace(os.sep, '.')
    if pkg == '.':
        pkg = ''
    mods = set()
    for f in files:
        if f.endswith('.py') and f != '__init__.py':
            mods.add(f[:-3])
    if mods:
        pkg_modules[pkg] = mods

print('Packages found:')
for pkg, mods in pkg_modules.items():
    label = pkg if pkg else '(root)'
    print('  {}: {}'.format(label, mods))

# Find bad imports: using bare module name when a sibling module in same package exists
fixes = []
for root, dirs, files in os.walk('src'):
    pkg = os.path.relpath(root, 'src').replace(os.sep, '.')
    if pkg == '.':
        pkg = ''
    for f in files:
        if not f.endswith('.py'):
            continue
        p = os.path.join(root, f)
        with open(p, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('from '):
                parts = stripped.split()
                if len(parts) >= 2:
                    mod = parts[1]
                    # If this module exists in the SAME package but import is bare
                    if mod in pkg_modules.get(pkg, set()) and '.' not in mod:
                        # Should use relative import or qualified
                        fixes.append((p, i, stripped, mod, pkg))

print('\nBad imports found:')
for p, i, old, mod, pkg in fixes:
    print('  {}:{}: {} -> should be relative'.format(p, i+1, old))

# Now also find cross-package imports that are bare
for root, dirs, files in os.walk('src'):
    pkg = os.path.relpath(root, 'src').replace(os.sep, '.')
    if pkg == '.':
        pkg = ''
    for f in files:
        if not f.endswith('.py'):
            continue
        p = os.path.join(root, f)
        with open(p, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('from '):
                parts = stripped.split()
                if len(parts) >= 2:
                    mod = parts[1]
                    if '.' in mod:
                        continue
                    # Check if this module exists in another package
                    for other_pkg, other_mods in pkg_modules.items():
                        if other_pkg != pkg and mod in other_mods:
                            full = other_pkg + '.' + mod if other_pkg else mod
                            fixes.append((p, i, stripped, mod, other_pkg))
                            print('  {}:{}: {} -> from {} import'.format(p, i+1, stripped, full))

print('\nTotal: {}'.format(len(fixes)))
