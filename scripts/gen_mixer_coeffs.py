import re, sys, itertools
src = open(sys.argv[1]).read()
def block(name):
    m = re.search(r'static const (?:int|double) %s\[[^\]]*\](?:\[\d+\])? = \{(.*?)\n\};' % name, src, re.S)
    return m.group(1)
powers = [tuple(int(x) for x in re.findall(r'-?\d+', row)) for row in re.findall(r'\{([^{}]*)\}', block('POWERS'))]
coef = [tuple(float(x) for x in row.split(',') if x.strip()) for row in re.findall(r'\{([^{}]*)\}', block('COEF'))]
m = re.search(r'static const double INTERCEPT\[3\] = \{([^}]*)\}', src)
intercept = tuple(float(x) for x in m.group(1).split(',') if x.strip())
assert len(powers) == 330 and len(coef) == 330 and all(len(c) == 3 for c in coef) and len(intercept) == 3
# sklearn PolynomialFeatures ordering: by degree, then combinations_with_replacement
gen = []
for d in range(5):
    for combo in itertools.combinations_with_replacement(range(7), d):
        p = [0]*7
        for i in combo: p[i] += 1
        gen.append(tuple(p))
assert gen == powers, "POWERS ordering differs from generated ordering"
out = sys.argv[2]
with open(out, 'w') as f:
    f.write('"""FilamentMixer degree-4 polynomial coefficients.\n\n'
            'Generated from justinh-rahb/filament-mixer cpp/filament_mixer.h\n'
            '(commit 22a23e9ed479f06b4ab57998b82f5e08093bb406), identical to the copy\n'
            'Snapmaker Orca vendors as src/libslic3r/filament_mixer_model.h.\n\n'
            'MIT License, Copyright (c) 2026 Justin Hayes. See NOTICE.\n\n'
            'Feature ordering (POWERS) matches sklearn PolynomialFeatures(degree=4) over\n'
            'inputs (r1, g1, b1, r2, g2, b2, t) and is regenerated in model.py.\n'
            'Do not edit by hand.\n"""\n\n')
    f.write('INTERCEPT = (%s)\n\n' % ', '.join(repr(x) for x in intercept))
    f.write('COEF = (\n')
    for c in coef:
        f.write('    (%s),\n' % ', '.join(repr(x) for x in c))
    f.write(')\n')
print('ok', len(coef))
