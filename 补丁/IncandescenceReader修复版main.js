const { app, BrowserWindow, protocol, net } = require('electron');
const path = require('path');
const fs   = require('fs');
const https = require('https');

// 便携化：所有可写数据（缓存、偏好、在线账号清单）集中到 exe 旁边的 data/ 一个目录，
// 不散落在根目录，删掉整个文件夹就等于彻底卸载。
// 目录不可写时（例如解压到 Program Files）静默退回系统默认路径。
(() => {
  if (!app.isPackaged) return;
  try {
    const base = path.join(path.dirname(app.getPath('exe')), 'data');
    fs.mkdirSync(base, { recursive: true });
    fs.accessSync(base, fs.constants.W_OK);
    app.setPath('userData', base);
    app.setPath('sessionData', base);
  } catch (e) {
    console.error('[portable] 数据目录不可写，使用系统默认路径:', e.message);
  }
})();

// 组织的线上账号清单。缓存一份到 userData，冷启动先出列表再后台刷新；
// 断网时仍能看到上次同步的内容（条目会被标成离线不可点）。
const ONLINE_LIST_URL = 'https://twitterarchiver.github.io/home/repos.json';

function onlineCachePath() {
  return path.join(app.getPath('userData'), 'online-accounts.json');
}

function fetchOnlineList() {
  return new Promise((resolve, reject) => {
    const req = https.get(ONLINE_LIST_URL, { timeout: 20000 }, res => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error('HTTP ' + res.statusCode));
      }
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', c => buf += c);
      res.on('end', () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
  });
}

// ── 数据根目录解析
//    打包后：resources/ 目录是数据所在的位置（accounts/ 或 wayback_snapshots/ 都在这里）
//    开发时：项目根目录（同 main.js 所在目录）
function getDataBaseDir() {
  if (app.isPackaged) {
    return process.resourcesPath;
  }
  return __dirname;
}

// ── 注册自定义协议 incr://，让 Reader.html 通过它读取本地文件
// 这样完全绕开 file:// 的跨域限制，fetch() 正常工作
//
// URL 路由规则：
//   incr://local/_accounts                      → 动态扫描 accounts/ 下所有子目录，
//                                                  读取每个的 profile.json 合成账号清单 JSON
//   incr://local/accounts/AnIncandescence/...   → DATA/accounts/AnIncandescence/...
//   incr://local/wayback_snapshots/...          → DATA/wayback_snapshots/... (向后兼容旧目录)
//   incr://local/Reader.html / 其他静态资源      → __dirname/...
const MIME = {
  '.html': 'text/html', '.htm': 'text/html',
  '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.txt': 'text/plain',
};

function listLocalAccounts(dataBase) {
  const accountsDir = path.join(dataBase, 'accounts');
  const list = [];
  if (!fs.existsSync(accountsDir)) return list;
  for (const d of fs.readdirSync(accountsDir, { withFileTypes: true })) {
    if (!d.isDirectory() || d.name.startsWith('.')) continue;
    const pf = path.join(accountsDir, d.name, 'wayback_snapshots', 'profile.json');
    if (!fs.existsSync(pf)) continue;
    try {
      const prof = JSON.parse(fs.readFileSync(pf, 'utf8'));
      list.push({
        dir: d.name,
        name:     prof.name     || d.name,
        username: prof.username || d.name,
        bio:      prof.bio      || '',
        avatar:   prof.avatar   ? `accounts/${d.name}/wayback_snapshots/${prof.avatar}` : '',
        banner:   prof.banner   ? `accounts/${d.name}/wayback_snapshots/${prof.banner}` : '',
      });
    } catch (e) {
      console.error(`[incr] 解析 ${pf} 失败:`, e.message);
    }
  }
  return list;
}

function registerProtocol() {
  protocol.handle('incr', async (request) => {
    const urlPath = decodeURIComponent(new URL(request.url).pathname.replace(/^\//, ''));
    const dataBase = getDataBaseDir();

    // 网络代理：incr://local/_net/<host>/<path> → https://<host>/<path>
    if (urlPath.startsWith('_net/')) {
      const rest = urlPath.slice(5);
      const host = rest.split('/')[0];
      if (host !== 'twitterarchiver.github.io') {
        console.error('[incr] 拒绝代理非白名单主机:', host);
        return new Response('forbidden', { status: 403 });
      }
      try {
        return await net.fetch('https://' + rest, {
          headers: request.headers,   // 透传 Range 等条件请求头
        });
      } catch (e) {
        console.error('[incr] 代理失败', rest, e.message);
        return new Response('bad gateway', { status: 502 });
      }
    }

    // 虚拟路径：线上账号清单。优先取网络，失败回落到本地缓存
    if (urlPath === '_online_accounts') {
      try {
        const list = await fetchOnlineList();
        try { fs.writeFileSync(onlineCachePath(), JSON.stringify(list), 'utf8'); } catch (e) {}
        return Response.json({ list, offline: false });
      } catch (err) {
        console.error('[incr] 拉取线上账号清单失败:', err.message);
        try {
          return Response.json({ list: JSON.parse(fs.readFileSync(onlineCachePath(), 'utf8')), offline: true });
        } catch (e) {
          return Response.json({ list: [], offline: true });
        }
      }
    }

    // 虚拟路径：扫 accounts/ 下所有子目录，组合 profile.json 生成账号清单
    if (urlPath === '_accounts' || urlPath === '_accounts.json') {
      try {
        return Response.json(listLocalAccounts(dataBase));
      } catch (e) {
        console.error('[incr] 扫描 accounts/ 失败:', e);
        return Response.json([]);
      }
    }


    // 实文件：支持 Range 请求，供 Chromium 视频播放器分段读取
    const filePath = (urlPath.startsWith('accounts/') || urlPath.startsWith('wayback_snapshots/'))
      ? path.join(dataBase, urlPath)
      : path.join(__dirname, urlPath);

    try {
      const buf = fs.readFileSync(filePath);
      const mime = MIME[path.extname(filePath).toLowerCase()]
        || 'application/octet-stream';
      const size = buf.length;
      const range = request.headers.get('range');

      const headers = {
        'content-type': mime,
        'accept-ranges': 'bytes',
      };

      if (range) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(range.trim());

        if (!match || (!match[1] && !match[2]) || size === 0) {
          return new Response(null, {
            status: 416,
            headers: {
              ...headers,
              'content-range': `bytes */${size}`,
            },
          });
        }

        let start;
        let end;

        if (!match[1]) {
          // 后缀范围，例如 bytes=-500
          const suffixLength = Number(match[2]);
          start = Math.max(size - suffixLength, 0);
          end = size - 1;
        } else {
          start = Number(match[1]);
          end = match[2] ? Number(match[2]) : size - 1;
          end = Math.min(end, size - 1);
        }

        if (start >= size || start > end) {
          return new Response(null, {
            status: 416,
            headers: {
              ...headers,
              'content-range': `bytes */${size}`,
            },
          });
        }

        const chunk = buf.subarray(start, end + 1);

        return new Response(
          request.method === 'HEAD' ? null : chunk,
          {
            status: 206,
            headers: {
              ...headers,
              'content-length': String(chunk.length),
              'content-range': `bytes ${start}-${end}/${size}`,
            },
          }
        );
      }

      headers['content-length'] = String(size);

      return new Response(
        request.method === 'HEAD' ? null : buf,
        { status: 200, headers }
      );
    } catch (e) {
      return new Response('not found', { status: 404 });
    }

  });
}

function createWindow() {
  const win = new BrowserWindow({
    width:  1440,
    height: 900,
    minWidth:  900,
    minHeight: 600,
    title: '白炽阅读器 · IncandescenceReader',
    backgroundColor: '#000000',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      // 允许 incr:// 协议页面访问本地资源
      webviewTag: false,
    },
    // 无边框可选，保留系统标题栏更稳定
    frame: true,
    show: false, // 加载完再显示，避免白屏闪烁
  });

  win.once('ready-to-show', () => win.show());

  win.loadURL('incr://local/Reader.html');

  // 开发时打开 DevTools（打包后注释掉）
  // win.webContents.openDevTools();
}

// 必须在 app ready 之前调用，否则新版 Electron 中自定义协议权限不生效
protocol.registerSchemesAsPrivileged([
  { scheme: 'incr', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } }
]);

app.whenReady().then(() => {
  registerProtocol();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});