// Runs the Python pipeline (web/py/*.py) in Pyodide, off the main thread.
// Protocol: main posts {id, cmd, args}; worker replies {id, ok, result|error}.
// Progress goes out as {type: "status"|"log", text} with no id.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.3/full/";
importScripts(PYODIDE + "pyodide.js");
importScripts("https://cdn.jsdelivr.net/npm/earcut@2.2.4/dist/earcut.min.js");

const status = (text) => postMessage({ type: "status", text });
let py, app;

const ready = (async () => {
  status("Downloading Python…");
  py = await loadPyodide({ indexURL: PYODIDE });
  status("Loading geometry libraries…");
  await py.loadPackage(["numpy", "shapely", "matplotlib", "networkx", "lxml", "micropip"], {
    messageCallback: (m) => /^Loading /.test(m) && status(m.replace(/, /g, " · ")),
  });
  status("Installing trimesh…");
  await py.runPythonAsync('import micropip\nawait micropip.install("trimesh==4.12.2")');
  py.FS.mkdirTree("/py");
  for (const f of ["webapp.py", "extrude_map.py", "verify_plate.py"]) {
    const r = await fetch(`py/${f}`, { cache: "no-cache" });
    if (!r.ok) throw new Error(`could not load py/${f}`);
    py.FS.writeFile(`/py/${f}`, await r.text());
  }
  status("Starting pipeline…");
  py.runPython('import sys; sys.path.insert(0, "/py")');
  app = py.pyimport("webapp");
  app.set_logger((t) => postMessage({ type: "log", text: t }));
  app.set_triangulator(self.earcut);
  py.runPython("import extrude_map, verify_plate, trimesh, matplotlib.pyplot");
  status("ready");
})();

onmessage = async (e) => {
  const { id, cmd, args } = e.data;
  try {
    await ready;
    if (cmd === "load") return reply(id, JSON.parse(app.load(args.text, args.nameProp)));
    if (cmd === "match") return reply(id, JSON.parse(app.match(args.quiz)));
    if (cmd === "prepare") return reply(id, JSON.parse(app.prepare(JSON.stringify(args))));
    if (cmd === "build") {
      const rep = JSON.parse(app.build(JSON.stringify(args)));
      const transfer = [];
      const grab = (p) => {
        const b = py.FS.readFile(p);
        transfer.push(b.buffer);
        return b;
      };
      rep.data = Object.fromEntries(Object.entries(rep.files).map(([k, p]) => [k, grab(p)]));
      rep.stlData = rep.stls.map((p) => ({ file: p.split("/").pop(), data: grab(p) }));
      return reply(id, rep, transfer);
    }
    throw new Error(`unknown command ${cmd}`);
  } catch (err) {
    const msg = String((err && err.message) || err);
    postMessage({ id, ok: false, error: msg, fatal: /fatal error|Pyodide already fatally failed/i.test(msg) });
  }
};

function reply(id, result, transfer = []) {
  postMessage({ id, ok: true, result }, transfer);
}
