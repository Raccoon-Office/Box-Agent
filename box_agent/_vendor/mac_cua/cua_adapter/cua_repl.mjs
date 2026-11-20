// Adapter evaluator only. Python owns MCP, driver access and the wall-clock deadline.
import repl from 'node:repl';
import net from 'node:net';
import readline from 'node:readline';
import { PassThrough } from 'node:stream';
import { AsyncLocalStorage } from 'node:async_hooks';
import { inspect } from 'node:util';

export async function selfTest() {
    const r = repl.start({input: new PassThrough(), output: new PassThrough(), terminal: false, prompt: ''});
    const evaluate = code => new Promise((resolve, reject) =>
        r.eval(code, r.context, 'self-test', (e, v) => e ? reject(e) : resolve(v)));
    try {
        await evaluate('let app = await Promise.resolve({name:"TextEdit"}); const text = "你好";');
        if (await evaluate('app.name + text') !== 'TextEdit你好') throw Error('Persistent REPL failed');
    } finally { r.close(); }
}

if (process.argv.includes('--self-test')) {
    await selfTest();
    process.stdout.write('CUA_REPL_OK\n');
} else {
    const capability = process.env.MAC_CUA_CAPABILITY || 'all';
    const socket = new net.Socket({fd: Number(process.env.MAC_CUA_REPL_FD), readable: true, writable: true});
    const send = value => socket.write(JSON.stringify(value) + '\n');
    const scope = new AsyncLocalStorage();
    let active = null;
    let serial = 0;
    const pending = new Map();
    const context = () => {
        const cell = scope.getStore();
        if (!cell || active !== cell || cell.closed) throw Error('This JS call has ended; await all CUA actions');
        return cell;
    };
    const emit = (content, image_dimensions) => {
        const cell = context();
        send({kind: 'emit', cell: cell.id, content, image_dimensions});
    };
    const write = value => emit({type: 'text', text: typeof value === 'string' ? value : inspect(value, {depth: 8, colors: false})});
    const emitImage = async value => {
        const bytes = value instanceof Uint8Array ? value : value?.bytes;
        if (!(bytes instanceof Uint8Array)) throw TypeError('emitImage expects Uint8Array or {bytes, mimeType}');
        const mimeType = value?.mimeType || (bytes[0] === 0xff && bytes[1] === 0xd8 ? 'image/jpeg' : 'image/png');
        emit({type: 'image', data: Buffer.from(bytes).toString('base64'), mimeType});
    };
    const call = (method, handle, args = {}) => {
        const cell = context();
        const id = ++serial;
        const promise = new Promise((resolve, reject) => {
            pending.set(id, {resolve, reject, cell});
            send({kind: 'call', cell: cell.id, id, method, handle, args});
        });
        // An unawaited rejection must not kill the persistent process.
        promise.catch(() => {});
        return promise;
    };
    const options = (value, allowed) => {
        if (value === undefined) return {};
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw TypeError('Options must be an object');
        for (const [key, v] of Object.entries(value)) {
            if (!allowed.includes(key) || typeof v !== 'boolean') throw TypeError(`Unsupported option: ${key}`);
        }
        return value;
    };
    const modifierAliases = {super: 'cmd', cmd: 'cmd', command: 'cmd', meta: 'cmd',
        ctrl: 'ctrl', control: 'ctrl', alt: 'option', option: 'option', shift: 'shift', fn: 'fn'};
    const modifierBits = {option: 1, ctrl: 2, cmd: 4, shift: 8};
    // Keep backend key names at the boundary. Delete retains its existing backend meaning;
    // Backspace and ForwardDelete express an unambiguous direction on either surface.
    const keyNames = {
        return: ['return', 'Enter'], enter: ['return', 'Enter'],
        kpenter: ['return', 'Enter'], numpadenter: ['return', 'Enter'],
        left: ['left', 'ArrowLeft'], arrowleft: ['left', 'ArrowLeft'], leftarrow: ['left', 'ArrowLeft'],
        right: ['right', 'ArrowRight'], arrowright: ['right', 'ArrowRight'], rightarrow: ['right', 'ArrowRight'],
        up: ['up', 'ArrowUp'], arrowup: ['up', 'ArrowUp'], uparrow: ['up', 'ArrowUp'],
        down: ['down', 'ArrowDown'], arrowdown: ['down', 'ArrowDown'], downarrow: ['down', 'ArrowDown'],
        esc: ['escape', 'Escape'], escape: ['escape', 'Escape'],
        backspace: ['delete', 'Backspace'], bksp: ['delete', 'Backspace'], delete: ['delete', 'Delete'],
        forwarddelete: ['forward_delete', 'Delete'], fwddelete: ['forward_delete', 'Delete'],
        deleteforward: ['forward_delete', 'Delete'], del: ['forward_delete', 'Delete'],
        tab: ['tab', 'Tab'], space: ['space', ' '], ' ': ['space', ' '],
        plus: ['=', '+'], '+': ['=', '+'],
        fn: ['fn', null], capslock: ['capslock', null],
        home: ['home', 'Home'], end: ['end', 'End'],
        prior: ['pageup', 'PageUp'], pageup: ['pageup', 'PageUp'],
        pgup: ['pageup', 'PageUp'],
        next: ['pagedown', 'PageDown'], pagedown: ['pagedown', 'PageDown'],
        pgdn: ['pagedown', 'PageDown'], pgdown: ['pagedown', 'PageDown'],
        shift: ['shift', 'Shift'], ctrl: ['ctrl', 'Control'], control: ['ctrl', 'Control'],
        alt: ['option', 'Alt'], option: ['option', 'Alt'],
        super: ['cmd', 'Meta'], cmd: ['cmd', 'Meta'], command: ['cmd', 'Meta'], meta: ['cmd', 'Meta'],
        win: ['cmd', 'Meta'],
    };
    // US physical keys: Shift changes the event character, never the caller's
    // letter casing. Keep this at the shared boundary so both backends agree.
    const physicalKeys = "abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./";
    const shiftedKeys = Object.fromEntries([..."`1234567890-=[]\\;',./"].map((key, index) =>
        [key, [...'~!@#$%^&*()_+{}|:"<>?'][index]]));
    const parsePressKey = (args, receiver) => {
        const invalid = reason => new TypeError(`InvalidArguments: ${receiver}.pressKey: ${reason}.\n`
            + 'Expected: pressKey(key: string), with exactly one argument.\n'
            + 'Example for Command+S: pressKey("super+s").\n'
            + 'This pressKey call was rejected before dispatch; no input was sent.');
        const [key] = args;
        if (args.length !== 1 || typeof key !== 'string' || key.length === 0) {
            throw invalid('expected exactly one nonempty key string');
        }
        const parts = key === '+' ? ['plus'] : key.split('+');
        const base = parts.pop();
        if (!base || parts.some(part => !Object.hasOwn(modifierAliases, part.toLowerCase()))) {
            throw invalid('use modifiers followed by one key, e.g. super+s or super+plus');
        }
        const modifiers = [...new Set(parts.map(part => modifierAliases[part.toLowerCase()]))];
        if (receiver === 'Tab' && modifiers.includes('fn')) {
            throw invalid('Fn is unsupported by browser input');
        }
        // Match macOS driver canonical_key_name for named keys, preserving
        // literal Space and single-character physical keys such as "-".
        let lower = base === ' ' ? base : base.trim().toLowerCase();
        if (lower.length > 1) lower = lower.replace(/[ _-]/g, '');
        let names;
        if (lower === 'plus' || lower === '+') {
            if (!modifiers.includes('shift')) modifiers.push('shift');
        }
        if (Object.hasOwn(keyNames, lower)) names = keyNames[lower];
        else if (/^f(?:[1-9]|1[0-2])$/.test(lower)) names = [lower, null];
        else if (lower.length === 1 && lower.charCodeAt(0) < 128 && physicalKeys.includes(lower)) names = [lower, lower];
        else throw invalid(`unknown key ${JSON.stringify(base)}; use typeText for exact text`);
        if (receiver === 'Tab' && (names[1] === null || ['shift', 'ctrl', 'option', 'cmd'].includes(names[0]))) {
            throw invalid(`key ${JSON.stringify(base)} is unsupported by browser input`);
        }
        if (names[0].length === 1 && physicalKeys.includes(names[0])) {
            const physical = names[0];
            names = [physical, modifiers.includes('shift')
                ? (shiftedKeys[physical] || physical.toUpperCase()) : physical];
        }
        return {native: [...modifiers, names[0]].join('+'), key: names[1],
            modifiers: modifiers.reduce((mask, name) => mask | (modifierBits[name] || 0), 0)};
    };
    let documentationShown = false;
    let documentationPending = null;
    const writeDocumentation = async (force = false) => {
        context();
        if (!force && documentationShown) return;
        if (!documentationPending) {
            documentationPending = (async () => {
                const documentation = await call('documentation');
                write(documentation);
                documentationShown = true;
            })().finally(() => { documentationPending = null; });
        }
        await documentationPending;
    };
    class App {
        #handle;
        constructor(handle) { this.#handle = handle; }
        async listWindows(opts) {
            opts = options(opts, ['emit']);
            const windows = await call('listWindows', this.#handle);
            if (opts.emit !== false) write(windows);
            return windows;
        }
        async #observe(method, opts) {
            opts = options(opts, method === 'getScreenshot' ? ['emit'] : ['emit', 'disableDiffing']);
            const result = await call(method, this.#handle, {disableDiffing: opts.disableDiffing || false});
            const screenshot = result.image ? Uint8Array.from(Buffer.from(result.image.data, 'base64')) : undefined;
            if (opts.emit !== false) {
                if (result.state !== undefined) write(result.state);
                if (result.image) emit(result.image, result.image_dimensions);
                else if (method === 'getAXStateAndScreenshot') write('Screenshot unavailable');
            }
            if (method === 'getScreenshot') return screenshot;
            if (method === 'getAXState') return result.state;
            return {state: result.state, ...(screenshot ? {screenshot} : {})};
        }
        getAXState(opts) { return this.#observe('getAXState', opts); }
        getScreenshot(opts) { return this.#observe('getScreenshot', opts); }
        getAXStateAndScreenshot(opts) { return this.#observe('getAXStateAndScreenshot', opts); }
        async click(target, options = {}) {
            const result = await call('click', this.#handle, {target, options});
            if (result?.warning) write(result.warning);
        }
        async drag(from, to) { await call('drag', this.#handle, {from, to}); }
        async pressKey(key) {
            const parsed = parsePressKey(arguments, 'App');
            await call('pressKey', this.#handle, {key: parsed.native});
        }
        async typeText(text) { await call('typeText', this.#handle, {text}); }
        async setValue(elementIndex, value) { await call('setValue', this.#handle, {target: elementIndex, value}); }
        async scroll(target, direction, pages = 1) { await call('scroll', this.#handle, {target, direction, pages}); }
        async paste(text, options = {}) { await call('paste', this.#handle, {text, options}); }
    }
    function writeBrowserState(state) {
        const {dom, ...page} = state;
        write(JSON.stringify(page) + '\n' +
            `DOM: ${dom.mode}, observation=${dom.observationId}, base=${dom.baseId}, scope=${dom.scope}, complete=${dom.complete}` +
            (dom.reason ? '\n' + dom.reason : '') + (dom.text ? '\n' + dom.text : ''));
    }
    function methodOptions(value, keys, method) {
        if (!value || typeof value !== 'object' || Array.isArray(value) ||
            Object.keys(value).some(key => !keys.includes(key))) {
            throw TypeError('InvalidArguments: ' + method + ' options: ' + keys.join(', '));
        }
        return value;
    }
    class Tab {
        #handle;
        constructor(handle) { this.#handle = handle; }
        async #observe(method, opts) {
            opts = options(opts, method === 'getScreenshot' ? ['emit'] : ['emit', 'disableDiffing']);
            const result = await call('browser.' + method, this.#handle, opts);
            const screenshot = result.image ? Uint8Array.from(Buffer.from(result.image.data, 'base64')) : undefined;
            if (opts.emit !== false) {
                if (method !== 'getScreenshot') writeBrowserState(result.state);
                if (result.image) emit(result.image, [result.state?.pixels?.width, result.state?.pixels?.height]);
            }
            if (method === 'getScreenshot') return screenshot;
            if (method === 'getState') return result.state;
            return {state: result.state, screenshot};
        }
        getState(opts) { return this.#observe('getState', opts); }
        getScreenshot(opts) { return this.#observe('getScreenshot', opts); }
        getStateAndScreenshot(opts) { return this.#observe('getStateAndScreenshot', opts); }
        async readText() {
            if (arguments.length !== 0) throw Error('InvalidArguments: readText takes no arguments');
            const value = await call('browser.readText', this.#handle);
            write(value);
            return value;
        }
        async #action(method, args) {
            const result = await call('browser.' + method, this.#handle, args);
            if (result?.notice) write(result.notice);
            if (result?.page) {
                write(result.page);
                return result.page;
            }
        }
        async goto(url) { return this.#action('goto', {url}); }
        async back() { await this.#action('back'); }
        async activate() { await call('browser.activate', this.#handle); }
        async click(target, options = {}) { await this.#action('click', {target, options}); }
        async fill(target, text) { await this.#action('fill', {target, text}); }
        async hover(target) { await this.#action('hover', {target}); }
        async typeText(text) { await call('browser.typeText', this.#handle, {text}); }
        async pressKey(key) {
            const parsed = parsePressKey(arguments, 'Tab');
            await this.#action('pressKey', {key: parsed.key, modifiers: parsed.modifiers});
        }
        async scroll(opts) {
            const {x, y, deltaX = 0, deltaY = 0} = methodOptions(opts, ['x', 'y', 'deltaX', 'deltaY'], 'scroll');
            if (![deltaX, deltaY].every(Number.isFinite) || !(deltaX || deltaY) ||
                ((x !== undefined || y !== undefined) && ![x, y].every(Number.isFinite))) {
                throw TypeError('InvalidScroll: supply a nonzero finite deltaX or deltaY and optionally both finite x and y');
            }
            await this.#action('scroll', {x, y, deltaX, deltaY});
        }
        async selectOption(target, values) { await this.#action('selectOption', {target, values}); }
        async waitForText(text, opts = {}) {
            const {state = 'visible'} = methodOptions(opts, ['state'], 'waitForText');
            await this.#action('waitForText', {text, state});
        }
        async waitForLoad({timeoutMs = 15000} = {}) { await call('browser.waitForLoad', this.#handle, {timeoutMs}); }
        async waitForElement(selector, {timeoutMs = 10000, visible = false} = {}) {
            await call('browser.waitForElement', this.#handle, {selector, timeoutMs, visible});
        }
        async evaluate(expression) {
            const value = await call('browser.evaluate', this.#handle, {expression});
            write(value);
            return value;
        }
        async uploadFile(selector, path) { await call('browser.uploadFile', this.#handle, {selector, path}); }
        async handleDialog(opts) {
            const {accept, promptText} = methodOptions(opts, ['accept', 'promptText'], 'handleDialog');
            await this.#action('handleDialog', {accept, promptText});
        }
        async close() { await call('browser.close', this.#handle); }
    }
    function exposeBrowserMethods(implementation, methods) {
        if (methods === undefined) return implementation;
        const prototype = Object.fromEntries(
            methods.map(name => [name, implementation[name].bind(implementation)])
        );
        return Object.create(Object.freeze(prototype));
    }
    class Browser {
        #tabMethods;
        constructor(tabMethods) { this.#tabMethods = tabMethods; }
        async openAndRead(url) {
            const page = await call('browser.openAndRead', null, {url});
            write(page);
            return page;
        }
        async listTabs(opts) {
            opts = options(opts, ['emit']);
            const tabs = await call('browser.listTabs');
            if (opts.emit !== false) write(tabs);
            return tabs;
        }
        #bindTab(result) {
            if (result.notice) write(result.notice);
            else if (result.state.mode === 'managed') writeBrowserState(result.state);
            else write(result.state);
            return exposeBrowserMethods(new Tab(result.handle), this.#tabMethods);
        }
        async getTab(targetId) {
            const result = await call('browser.getTab', null, {targetId});
            return this.#bindTab(result);
        }
        async newTab(url = 'about:blank') {
            const result = await call('browser.newTab', null, {url});
            return this.#bindTab(result);
        }
    }
    const axElementId = (state, description) => {
        if (typeof state !== 'string' || typeof description !== 'string' || !description)
            throw TypeError('axElementId expects an AX state string and a nonempty element description');
        const matches = state.split(/\r?\n/).flatMap(line => {
            const match = line.match(/^[+~]?[ \t]*(\d+)[ \t]+(.+)$/);
            return match?.[2] === description ? [Number(match[1])] : [];
        });
        if (matches.length !== 1)
            throw Error(`Expected exactly one AX element matching ${JSON.stringify(description)}; found ${matches.length}`);
        return matches[0];
    };
    const cua = {
        axElementId,
        async rewriteDocumentation() { await writeDocumentation(true); },
        async getBrowser(browser) {
            await writeDocumentation();
            const result = await call('browser.getBrowser', null, {browser});
            write(result.tabs);
            return exposeBrowserMethods(new Browser(result.tabMethods), result.browserMethods);
        },
        async listApps(opts) {
            opts = options(opts, ['emit']);
            await writeDocumentation();
            const apps = await call('listApps');
            if (opts.emit !== false) write(apps);
            return apps;
        },
        async getState(opts) {
            opts = options(opts, ['emit']);
            await writeDocumentation();
            const apps = capability === 'browser' ? [] : await call('listApps');
            const browsers = capability === 'cua' ? [] : await call('browser.availability');
            const state = capability === 'browser' ? {browsers} : capability === 'cua' ? {apps} : {apps, browsers};
            if (opts.emit !== false) write(state);
            return state;
        },
        async getApp(app) {
            await writeDocumentation();
            const result = await call('getApp', null, {app});
            write(result.state);
            return new App(result.handle);
        },
    };
    if (capability === 'cua') delete cua.getBrowser;
    if (capability === 'browser') { delete cua.getApp; delete cua.listApps; }
    const output = new PassThrough();
    const r = repl.start({input: new PassThrough(), output, terminal: false, prompt: '', useGlobal: false});
    Object.assign(r.context, {cua, Uint8Array, nodeRepl: {write, emitImage}});
    // Console text never contaminates the internal RPC socket or the MCP stdout.
    r.context.console = {log: (...args) => write(args.map(x => typeof x === 'string' ? x : inspect(x)).join(' ')),
        error: (...args) => write(args.map(String).join(' '))};
    const finish = (error, cell = scope.getStore()) => {
        if (!cell || active !== cell || cell.closed) return;
        cell.closed = true;
        const unawaited = [...pending.values()].some(p => p.cell === cell);
        send({kind: 'done', cell: cell.id, error: error ? String(error.stack || error) :
            unawaited ? 'CUA requests were not awaited; inspect state before continuing' : null});
        active = null;
    };
    // Node REPL routes thrown errors through its domain instead of its eval callback.
    r._domain.on('error', error => finish(error));
    readline.createInterface({input: socket}).on('line', line => {
        const message = JSON.parse(line);
        if (message.kind === 'reply') {
            const p = pending.get(message.id);
            if (!p) return;
            pending.delete(message.id);
            if (p.cell.closed) { p.reject(Error('JS call ended')); return; }
            if (message.error) p.reject(Error(message.error));
            else p.resolve(message.value);
        } else if (message.kind === 'execute') {
            if (active) throw Error('Concurrent evaluation is not supported');
            const cell = {id: message.cell, closed: false};
            active = cell;
            scope.run(cell, () => {
                // End trailing // comments before Node wraps top-level await.
                try { r.eval(message.code + '\n', r.context, 'cua-cell', error => finish(error)); }
                catch (error) { finish(error); }
            });
        }
    });
    socket.on('close', () => process.exit(0));
}
