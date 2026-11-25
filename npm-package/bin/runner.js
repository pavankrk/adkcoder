'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawnSync } = require('node:child_process');

const pkg = require('../package.json');

function commandExists(cmd) {
  const probe = process.platform === 'win32' ? 'where' : 'which';
  const res = spawnSync(probe, [cmd], { stdio: 'ignore' });
  return res.status === 0;
}

function findUv() {
  if (commandExists('uv')) return 'uv';
  const candidates =
    process.platform === 'win32'
      ? [
          path.join(os.homedir(), '.local', 'bin', 'uv.exe'),
          path.join(os.homedir(), '.cargo', 'bin', 'uv.exe'),
          ...(process.env.LOCALAPPDATA
            ? [path.join(process.env.LOCALAPPDATA, 'uv', 'uv.exe')]
            : []),
        ]
      : [
          path.join(os.homedir(), '.local', 'bin', 'uv'),
          '/opt/homebrew/bin/uv',
          '/usr/local/bin/uv',
        ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function findPython311Plus() {
  const candidates = process.env.ADK_CODER_PYTHON
    ? [process.env.ADK_CODER_PYTHON]
    : process.platform === 'win32'
      ? ['python', 'py']
      : ['python3.13', 'python3.12', 'python3.11', 'python3', 'python'];

  for (const cmd of candidates) {
    if (!fs.existsSync(cmd) && !commandExists(cmd)) continue;
    const args =
      cmd === 'py'
        ? ['-3', '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)']
        : ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'];
    const res = spawnSync(cmd, args, { stdio: 'ignore' });
    if (res.status === 0) {
      return cmd === 'py' ? { cmd: 'py', prefix: ['-3'] } : { cmd, prefix: [] };
    }
  }
  return null;
}

function findBundledWheel() {
  const wheelDir = path.resolve(__dirname, '..', 'wheels');
  if (!fs.existsSync(wheelDir)) return null;
  const files = fs.readdirSync(wheelDir).filter((f) => f.endsWith('.whl'));
  return files.length ? path.join(wheelDir, files[0]) : null;
}

function findSourceCheckout() {
  const candidate = path.resolve(__dirname, '..', '..');
  return fs.existsSync(path.join(candidate, 'pyproject.toml')) ? candidate : null;
}

function venvBinDir(venvRoot) {
  return path.join(venvRoot, process.platform === 'win32' ? 'Scripts' : 'bin');
}

function resolveBin(venvRoot, name) {
  const ext = process.platform === 'win32' ? '.exe' : '';
  return path.join(venvBinDir(venvRoot), name + ext);
}

function wantsDesktop(entryName, argv = process.argv.slice(2)) {
  if (entryName === 'adk-coder-app') return true;
  return argv.some((a) => a === '--app' || a === '-app' || a === 'app');
}

function buildInstallSpec(includeDesktop = false) {
  const extras = includeDesktop ? 'local,desktop' : 'local';
  const wheel = findBundledWheel();
  if (wheel) {
    return `adk-coder[${extras}] @ ${pathToFileURL(wheel).href}`;
  }
  const checkout = findSourceCheckout();
  if (checkout) {
    return `${checkout}[${extras}]`;
  }
  return `adk-coder[${extras}]==${pkg.version}`;
}

function ensureDesktopDependency(venvRoot) {
  const py = resolveBin(venvRoot, 'python');
  if (!fs.existsSync(py)) return;
  const check = spawnSync(py, ['-c', 'import webview'], { stdio: 'ignore' });
  if (check.status === 0) return;
  process.stderr.write('Installing pywebview for ADK Coder native desktop window...\n');
  const uvBin = findUv();
  if (uvBin) {
    spawnSync(uvBin, ['pip', 'install', '--python', py, 'pywebview>=5.3'], { stdio: 'inherit' });
  } else {
    const pip = resolveBin(venvRoot, 'pip');
    if (fs.existsSync(pip)) {
      spawnSync(pip, ['install', 'pywebview>=5.3'], { stdio: 'inherit' });
    }
  }
}

function wheelStamp(wheelPath) {
  if (!wheelPath || !fs.existsSync(wheelPath)) return null;
  const hash = crypto.createHash('sha256').update(fs.readFileSync(wheelPath)).digest('hex');
  return hash.slice(0, 16);
}

function ensureInstalled(entryName) {
  const dataBase =
    process.platform === 'win32'
      ? process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
      : process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  const venvRoot = path.join(dataBase, 'adk-coder', `venv-${pkg.version}`);
  const targetBin = resolveBin(venvRoot, entryName);
  const stampFile = path.join(venvRoot, '.wheel-stamp');
  const wheel = findBundledWheel();
  const currentStamp = wheelStamp(wheel);

  if (process.env.ADK_CODER_REINSTALL === '1' && fs.existsSync(venvRoot)) {
    fs.rmSync(venvRoot, { recursive: true, force: true });
  }

  const needDesktop = wantsDesktop(entryName);

  if (fs.existsSync(targetBin)) {
    // Fast-update only the adk-coder wheel when a rebuilt tarball ships a newer wheel
    if (currentStamp && wheel) {
      const savedStamp = fs.existsSync(stampFile)
        ? fs.readFileSync(stampFile, 'utf8').trim()
        : '';
      if (savedStamp !== currentStamp) {
        const uvBin = findUv();
        const py = resolveBin(venvRoot, 'python');
        const wheelUrl = pathToFileURL(wheel).href;
        if (uvBin) {
          spawnSync(uvBin, ['pip', 'install', '--python', py, '--no-deps', '--reinstall', wheelUrl], {
            stdio: 'ignore',
          });
        } else {
          const pip = resolveBin(venvRoot, 'pip');
          if (fs.existsSync(pip)) {
            spawnSync(pip, ['install', '--no-deps', '--force-reinstall', wheelUrl], {
              stdio: 'ignore',
            });
          }
        }
        try {
          fs.writeFileSync(stampFile, currentStamp, 'utf8');
        } catch (_) {}
      }
    }
    if (needDesktop) ensureDesktopDependency(venvRoot);
    return targetBin;
  }

  const installSpec = buildInstallSpec(needDesktop);
  fs.mkdirSync(path.dirname(venvRoot), { recursive: true });
  process.stderr.write(`Setting up adk-coder v${pkg.version} environment (first run)...\n`);

  const uvBin = findUv();
  if (uvBin) {
    const mk = spawnSync(uvBin, ['venv', venvRoot, '--python', '>=3.11'], {
      stdio: 'inherit',
    });
    if (mk.status !== 0) process.exit(mk.status || 1);
    const py = resolveBin(venvRoot, 'python');
    const inst = spawnSync(uvBin, ['pip', 'install', '--python', py, installSpec], {
      stdio: 'inherit',
    });
    if (inst.status !== 0) process.exit(inst.status || 1);
  } else {
    const py = findPython311Plus();
    if (!py) {
      process.stderr.write(
        'Error: adk-coder requires Python 3.11+ or uv on PATH.\n' +
          'Install uv (https://docs.astral.sh/uv/) or Python 3.11+ and retry.\n'
      );
      process.exit(1);
    }
    const mk = spawnSync(py.cmd, [...py.prefix, '-m', 'venv', venvRoot], {
      stdio: 'inherit',
    });
    if (mk.status !== 0) process.exit(mk.status || 1);
    const pip = resolveBin(venvRoot, 'pip');
    const inst = spawnSync(pip, ['install', '--upgrade', installSpec], {
      stdio: 'inherit',
    });
    if (inst.status !== 0) process.exit(inst.status || 1);
  }

  if (currentStamp) {
    try {
      fs.writeFileSync(stampFile, currentStamp, 'utf8');
    } catch (_) {}
  }

  return targetBin;
}

function run(entryName) {
  const binPath = ensureInstalled(entryName);
  const childEnv = {
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    ...process.env,
  };
  const res = spawnSync(binPath, process.argv.slice(2), {
    stdio: 'inherit',
    env: childEnv,
  });
  if (res.error) {
    process.stderr.write(`Failed to start ${entryName}: ${res.error.message}\n`);
    process.exit(1);
  }
  if (res.signal && process.platform !== 'win32') {
    process.kill(process.pid, res.signal);
  }
  process.exit(res.status === null ? 1 : res.status);
}

module.exports = { run, buildInstallSpec, findBundledWheel, findUv, wantsDesktop };
