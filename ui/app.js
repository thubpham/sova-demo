const F = n => n == null ? '—' : n >= 1e9 ? `${+(n / 1e9).toFixed(2)} tỷ` : n >= 1e6 ? `${+(n / 1e6).toFixed(1)}tr` : n.toLocaleString('vi-VN') + 'đ';
const INK = 'oklch(0.22 0.01 260)';
const GREEN = 'oklch(0.4 0.1 155)';
const RED = 'oklch(0.48 0.16 25)';
const AMBER = 'oklch(0.45 0.1 65)';
const RC = {
  fixed: { main: 'oklch(0.42 0.09 245)', tint: 'oklch(0.94 0.03 245)', badge: 'FIXED · quy tắc trong code', tag: 'QUY TẮC', sub: 'Code chọn bước tiếp theo' },
  llm: { main: 'oklch(0.45 0.15 305)', tint: 'oklch(0.945 0.035 305)', badge: 'LLM · Orchestrator tự chọn', tag: 'LLM · ly_do', sub: 'Orchestrator LLM chọn route và nêu lý do' }
};
const LABEL = { sales: 'Sales', inventory: 'Inventory', procurement: 'Procurement', finalize: 'Finalize', __end__: 'Kết thúc' };
const NODES = [['Orchestrator', 'orchestrator'], ['Sales', 'sales'], ['Inventory', 'inventory'], ['Procurement', 'procurement'], ['Cổng duyệt', 'approval_gate'], ['Finalize', 'finalize']];
const RUN_SCENARIOS = ['run1', 'run3', 'reject', 'instock'];
const money = v => v == null ? '?' : '$' + v.toFixed(4);
const num = v => (v ?? 0).toLocaleString('en-US');

async function call(path, options = {}) {
  const res = await fetch('/api' + path, { headers: { 'content-type': 'application/json' }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

function statusOf(view) {
  if (!view) return 'idle';
  return view.next.length ? 'paused' : 'done';
}

function buildItems(view, ctx) {
  if (!view) return [];
  const s = view.state, sc = ctx.scenario, items = [], seen = new Set();
  const corrected = (s.audit || []).some(e => e.node === 'inventory' && e.detail.startsWith('Hiệu chỉnh'));
  for (const e of s.audit || []) {
    if (e.node === 'orchestrator') {
      const m = e.detail.match(/^→ (\S+) \((\w+)\): ([\s\S]*)$/);
      items.push(m ? { k: 'orch', to: LABEL[m[1]] || m[1], why: m[3] } : { k: 'orch', to: '⚠ tuyến bị từ chối', why: e.detail });
      continue;
    }
    if (seen.has(e.node)) continue;
    seen.add(e.node);
    if (e.node === 'sales' && s.sales_check) {
      const c = s.sales_check;
      items.push({ k: 'work', n: 'Sales', t: 'Kiểm tra công nợ', model: ctx.model('sales_agent'), ok: c.hop_le, res: c.hop_le ? '✓ Hợp lệ' : '✕ Từ chối',
        facts: [['Khách hàng', `${sc.customer_id} · ${sc.ten_cong_ty}`], ['Giá trị đơn', F(sc.quantity * sc.gia_ban_vnd)], ['Hạn mức còn lại', F(c.cong_no_con_lai_vnd)]],
        note: c.ly_do, sum: c.ly_do });
    } else if (e.node === 'inventory' && s.stock_check) {
      const c = s.stock_check, short = c.thieu_hut;
      const head = short ? `Thiếu ${short} ${sc.don_vi}.` : 'Đủ hàng, không có thiếu hụt.';
      items.push({ k: 'work', n: 'Inventory', t: 'Đối chiếu tồn kho', model: ctx.model('inventory_agent'), ok: true, res: short ? '! Thiếu hàng' : '✓ Đủ hàng',
        facts: [['Sản phẩm', `${sc.sku} · ${sc.ten_san_pham}`], ['Tồn kho', `${c.ton_kho} ${sc.don_vi}`], ['Điểm đặt hàng lại', `${c.diem_dat_hang_lai} ${sc.don_vi}`], ['Thiếu hụt', `${short} ${sc.don_vi}`]],
        note: head + (corrected ? ' Kết quả của agent đã được hiệu chỉnh theo ERP.' : ' Kết quả đã được đối chiếu với ERP.'), sum: head });
    } else if (e.node === 'procurement' && s.draft_po) {
      const po = s.draft_po;
      items.push({ k: 'work', n: 'Procurement', t: 'Chọn nhà cung cấp, lập PO', model: ctx.model('procurement_agent'), ok: true, res: '✓ Đã lập PO',
        facts: [['Nhà cung cấp', `${po.vendor_id} · ${ctx.vendor(po.vendor_id)}`], ['Đơn giá', F(po.don_gia_vnd)], ['Số lượng', `${po.so_luong} ${sc.don_vi}`], ['Thành tiền', F(po.thanh_tien_vnd)], ['Giao trong', `${po.thoi_gian_giao_ngay} ngày`]],
        note: po.ly_do, sum: `${po.vendor_id}, ${F(po.thanh_tien_vnd)}, giao ${po.thoi_gian_giao_ngay} ngày` });
    } else if (e.node === 'approval_gate' && s.draft_po) {
      items.push({ k: 'gate', po: s.draft_po, decision: s.approvals[s.approvals.length - 1], pending: false });
    } else if (e.node === 'finalize') {
      const rejected = view.status === 'từ chối';
      items.push({ k: 'fin', ok: !rejected, res: (rejected ? '✕ ' : '✓ ') + (rejected ? 'Đã từ chối' : 'Hoàn tất'),
        facts: [['Trạng thái đơn', view.status], ['Ghi nhận', e.detail]], sum: e.detail });
    }
  }
  if (view.interrupt && s.draft_po) items.push({ k: 'gate', po: s.draft_po, decision: null, pending: true });
  return items;
}

function routePath(view) {
  if (!view) return '';
  const names = { sales: 'Sales', inventory: 'Inventory', procurement: 'Procurement', approval_gate: 'Approval', finalize: 'Finalize' };
  const path = [];
  for (const e of view.state.audit || []) {
    const n = names[e.node];
    if (n && path[path.length - 1] !== n) path.push(n);
  }
  return path.join(' → ');
}

document.addEventListener('alpine:init', () => {
  Alpine.data('demo', () => ({
    tab: 'run',
    scen: 'run1',
    router: 'fixed',
    scenarios: [],
    agents: [],
    vendors: {},
    runs: {},
    approver: 'truongphong.thumua@besgroup.vn',
    note: 'Đồng ý theo đề xuất của bộ phận thu mua',
    insp: null,
    usage: null,
    uf: 'all',
    uview: 'table',
    toast: '',
    sources: {},

    async init() {
      try {
        const [scenarios, agents] = await Promise.all([call('/scenarios'), call('/agents')]);
        this.scenarios = scenarios;
        this.agents = agents;
        await this.loadInspect();
        await this.loadRun(this.scenario().order_id);
      } catch (e) {
        this.flash('Không kết nối được API: ' + e.message);
      }
    },

    flash(text) {
      this.toast = text;
      clearTimeout(this._t);
      this._t = setTimeout(() => (this.toast = ''), 6000);
    },

    scenario(key) {
      return this.scenarios.find(s => s.key === (key || this.scen)) || {};
    },
    ctx(key) {
      return {
        scenario: this.scenario(key),
        model: id => ((this.agents.find(a => a.id === id) || {}).model || '').replace('anthropic:claude-', ''),
        vendor: id => this.vendors[id] || ''
      };
    },
    run(orderId) {
      return this.runs[orderId] || { view: null, status: 'idle', error: '', restarted: '' };
    },
    setRun(orderId, patch) {
      this.runs[orderId] = { ...this.run(orderId), ...patch };
    },
    get busy() {
      return Object.values(this.runs).some(r => r.status === 'running' || r.status === 'queued');
    },

    async loadRun(orderId) {
      if (!orderId) return;
      try {
        const view = await call(`/runs/${orderId}`);
        this.setRun(orderId, { view, status: statusOf(view) });
      } catch {
        if (this.run(orderId).status !== 'running') this.setRun(orderId, { view: null, status: 'idle' });
      }
    },

    listen(orderId) {
      this.sources[orderId]?.close();
      const es = new EventSource(`/api/runs/${orderId}/events`);
      this.sources[orderId] = es;
      es.onmessage = msg => {
        const e = JSON.parse(msg.data);
        if (e.run) this.setRun(orderId, { view: e.run });
        if (e.type === 'queued') this.setRun(orderId, { status: 'queued' });
        if (e.type === 'start' || e.type === 'resume') this.setRun(orderId, { status: 'running', error: '' });
        if (e.type === 'paused' || e.type === 'done' || e.type === 'error') {
          es.close();
          this.setRun(orderId, { status: e.type === 'error' ? 'error' : e.type, error: e.message || '' });
          if (e.type === 'error') this.flash(e.message);
          this.loadInspect();
          if (this.tab === 'us') this.loadUsage();
        }
      };
      es.onerror = () => {
        es.close();
        if (['running', 'queued'].includes(this.run(orderId).status)) setTimeout(() => this.loadRun(orderId), 1000);
      };
    },

    pick(patch) {
      Object.assign(this, patch);
      this.loadRun(this.scenario().order_id);
    },

    async start() {
      const sc = this.scenario();
      try {
        this.setRun(sc.order_id, { status: 'running', view: null, restarted: '', error: '' });
        await call('/runs', { method: 'POST', body: JSON.stringify({ scenario: this.scen, router: this.router }) });
        this.listen(sc.order_id);
      } catch (e) {
        this.setRun(sc.order_id, { status: 'idle' });
        this.loadRun(sc.order_id);
        this.flash(e.message);
      }
    },

    async decide(hanh_dong) {
      const id = this.scenario().order_id;
      try {
        await call(`/runs/${id}/resume`, { method: 'POST', body: JSON.stringify({ hanh_dong, nguoi_duyet: this.approver, ghi_chu: this.note }) });
        this.setRun(id, { status: 'running', restarted: '' });
        this.listen(id);
      } catch (e) {
        this.flash(e.message);
      }
    },

    async restart() {
      const id = this.scenario().order_id;
      try {
        const r = await call(`/runs/${id}/restart`, { method: 'POST' });
        this.setRun(id, { view: r.run, restarted: `Tiến trình mới đã nạp checkpoint: bước tiếp theo = (${r.next.map(n => `'${n}'`).join(', ')},), đã có ${r.audit_lines} dòng audit, ${r.snapshots} ảnh chụp.` });
      } catch (e) {
        this.flash(e.message);
      }
    },

    async resetAll() {
      try {
        await call('/reset', { method: 'POST' });
        Object.values(this.sources).forEach(s => s.close());
        this.runs = {};
        this.usage = null;
        this.uf = 'all';
        await this.loadInspect();
        this.flash('Đã đặt lại demo: dữ liệu ERP, checkpoint, bộ nhớ và usage.');
      } catch (e) {
        this.flash(e.message);
      }
    },

    async cmpStart() {
      try {
        const ids = await call('/compare', { method: 'POST' });
        for (const r of ['fixed', 'llm']) {
          this.setRun(ids[r].order_id, { status: 'queued', view: null, error: '' });
          this.listen(ids[r].order_id);
        }
      } catch (e) {
        this.flash(e.message);
      }
    },

    async loadInspect() {
      try {
        this.insp = await call('/inspect');
        this.vendors = Object.fromEntries(this.insp.erp.vendors.map(v => [v.vendor_id, v.ten_ncc]));
      } catch (e) {
        this.flash(e.message);
      }
    },

    async loadUsage() {
      try {
        this.usage = await call('/usage' + (this.uf === 'all' ? '' : `?order=${encodeURIComponent(this.uf)}`));
      } catch (e) {
        this.flash(e.message);
      }
    },

    openTab(k) {
      this.tab = k;
      if (k === 'insp') this.loadInspect();
      if (k === 'us') this.loadUsage();
      if (k === 'cmp') ['fixed', 'llm'].forEach(r => this.loadRun(`${this.scenario('compare').order_id}-${r}`));
      if (k === 'run') this.loadRun(this.scenario().order_id);
    },

    get tabs() {
      return [['run', 'Chạy đơn'], ['cmp', 'So sánh router'], ['insp', 'Kiểm tra'], ['us', 'LLM usage'], ['ag', 'Agent (chỉ đọc)']]
        .map(([k, label]) => ({ k, label, bg: this.tab === k ? INK : 'transparent', fg: this.tab === k ? '#fff' : 'oklch(0.35 0.01 260)' }));
    },
    get scens() {
      return this.scenarios.filter(s => RUN_SCENARIOS.includes(s.key)).map(s => ({
        ...s, line: `${s.customer_id} · ${s.quantity} × ${s.sku}`, bd: this.scen === s.key ? this.rc.main : 'oklch(0.9 0.006 90)'
      }));
    },
    get routers() {
      return ['fixed', 'llm'].map(k => ({ k, name: k === 'fixed' ? 'Fixed' : 'LLM', desc: RC[k].sub, bg: this.router === k ? RC[k].tint : '#fff', bd: this.router === k ? RC[k].main : 'oklch(0.9 0.006 90)', dot: RC[k].main }));
    },
    get cur() {
      return this.run(this.scenario().order_id);
    },
    get curRouter() {
      return this.cur.view?.router || this.router;
    },
    get rc() {
      return RC[this.curRouter] || RC.fixed;
    },
    get startLabel() {
      if (this.cur.status === 'running') return 'Đang chạy…';
      if (this.cur.view) return 'Đã chạy · đặt lại demo để chạy lại';
      return 'Chạy đơn';
    },
    get canStart() {
      return !this.busy && !this.cur.view && this.scenarios.length > 0;
    },
    get status() {
      const st = this.cur.status, rejected = this.cur.view?.status === 'từ chối';
      if (st === 'running') return { text: 'Đang chạy', bg: 'oklch(0.94 0.03 245)', fg: 'oklch(0.4 0.1 245)' };
      if (st === 'paused') return { text: 'Chờ phê duyệt', bg: 'oklch(0.93 0.06 80)', fg: 'oklch(0.4 0.1 65)' };
      if (st === 'error') return { text: 'Lỗi', bg: 'oklch(0.94 0.04 25)', fg: RED };
      if (st === 'done') return rejected ? { text: 'Đã từ chối', bg: 'oklch(0.94 0.04 25)', fg: 'oklch(0.45 0.16 25)' } : { text: 'Hoàn tất', bg: 'oklch(0.94 0.05 155)', fg: GREEN };
      return { text: 'Chưa chạy', bg: 'oklch(0.95 0.005 90)', fg: 'oklch(0.4 0.01 260)' };
    },
    get timeline() {
      return buildItems(this.cur.view, this.ctx());
    },
    get nodes() {
      const audit = this.cur.view?.state.audit || [], done = this.cur.status === 'done';
      const pending = !!this.cur.view?.interrupt;
      return NODES.map(([label, key]) => {
        const hit = audit.some(e => e.node === key) || (key === 'approval_gate' && pending);
        const skip = done && !hit;
        return { label, mark: hit ? '●' : skip ? '–' : '○', bg: hit ? this.rc.tint : '#fff', fg: hit ? this.rc.main : skip ? 'oklch(0.55 0.01 260)' : 'oklch(0.4 0.01 260)', bd: hit ? this.rc.main : 'oklch(0.85 0.008 90)', bs: skip ? 'dashed' : 'solid' };
      });
    },
    get stateJson() {
      const v = this.cur.view;
      if (!v) return JSON.stringify({ request: { order_id: this.scenario().order_id, customer_id: this.scenario().customer_id, sku: this.scenario().sku, quantity: this.scenario().quantity } }, null, 2);
      const { messages, ...rest } = v.state;
      return JSON.stringify({ ...rest, messages: (messages || []).map(m => `[${m.name}] ${m.content}`) }, null, 2);
    },
    gate(it) {
      const d = it.decision, rej = d && d.hanh_dong === 'reject';
      return {
        bg: it.pending ? 'oklch(0.97 0.03 85)' : rej ? 'oklch(0.97 0.02 25)' : 'oklch(0.97 0.025 155)',
        bd: it.pending ? 'oklch(0.8 0.1 80)' : rej ? 'oklch(0.75 0.1 25)' : 'oklch(0.8 0.07 155)',
        fg: it.pending ? AMBER : rej ? RED : GREEN,
        res: it.pending ? '⏸ Tạm dừng, chờ duyệt' : rej ? '✕ Từ chối' : '✓ Được duyệt',
        note: it.po.can_phe_duyet ? 'vượt ngưỡng, cần người duyệt' : '≤ 50tr: tự động duyệt',
        decText: d ? `${rej ? 'Từ chối' : 'Duyệt'} bởi ${d.nguoi_duyet}${d.ghi_chu ? '. Ghi chú: ' + d.ghi_chu : ''}` : ''
      };
    },
    resColor(it) {
      return it.ok ? GREEN : RED;
    },
    vendorName(id) {
      return this.vendors[id] || '';
    },
    F,

    get cols() {
      const base = this.scenario('compare').order_id;
      return ['fixed', 'llm'].map(r => {
        const id = `${base}-${r}`, run = this.run(id), its = buildItems(run.view, this.ctx('compare'));
        return {
          r, id, main: RC[r].main, tint: RC[r].tint, badge: RC[r].badge,
          sub: run.status === 'queued' ? 'đang chờ lượt' : run.status === 'running' ? 'đang chạy…' : run.view ? `${its.length} bước audit` : '',
          rows: its.map(x => x.k === 'orch' ? { title: 'Orchestrator → ' + x.to, text: x.why, res: r === 'llm' ? 'LLM' : 'quy tắc', rc: RC[r].main, bg: RC[r].tint }
            : x.k === 'work' ? { title: `${x.n} · ${x.t}`, text: x.sum, res: x.res, rc: this.resColor(x), bg: 'oklch(0.97 0.004 90)' }
            : x.k === 'gate' ? { title: 'Cổng phê duyệt', text: `${F(x.po.thanh_tien_vnd)} · ${this.gate(x).note}`, res: this.gate(x).res, rc: this.gate(x).fg, bg: 'oklch(0.97 0.004 90)' }
            : { title: 'Finalize', text: x.sum, res: x.res, rc: this.resColor(x), bg: 'oklch(0.97 0.004 90)' }),
          done: run.status === 'done', path: routePath(run.view), usage: run.view?.usage || ''
        };
      });
    },
    get cmpRunning() {
      return this.cols.some(c => ['running', 'queued'].includes(this.run(c.id).status));
    },
    get cmpDone() {
      return this.cols.every(c => c.done);
    },
    get cmpExists() {
      return this.cols.some(c => this.run(c.id).view);
    },
    get cmpBanner() {
      const match = this.cols[0].path === this.cols[1].path;
      return match ? { text: 'Kết quả: khớp. Hai router đi cùng một thứ tự bước.', bg: 'oklch(0.94 0.05 155)', fg: 'oklch(0.35 0.1 155)' } : { text: 'Kết quả: khác nhau.', bg: 'oklch(0.94 0.04 25)', fg: 'oklch(0.45 0.16 25)' };
    },

    get threads() {
      return (this.insp?.threads || []).map(t => {
        const rr = RC[t.router] || RC.fixed;
        return { ...t, label: (t.router || '?').toUpperCase(), rtint: rr.tint, rmain: rr.main, sc: t.status === 'đang chờ duyệt' ? AMBER : t.status === 'từ chối' ? RED : t.status === 'hoàn tất' ? GREEN : INK };
      });
    },
    get memory() {
      return Object.entries(this.insp?.memory || {}).map(([name, rows]) => ({
        name, rows: rows.map(m => ({ ...m, json: JSON.stringify(m.value), bg: m.flag ? 'oklch(0.93 0.06 155)' : 'transparent' }))
      }));
    },
    get erp() {
      const e = this.insp?.erp;
      if (!e) return [];
      return [
        { name: 'Khách hàng · công nợ', rows: e.customers.map(c => { const room = c.han_muc_cong_no_vnd - c.cong_no_hien_tai_vnd; return { id: c.customer_id, name: c.ten_cong_ty, val: `còn ${F(room)}`, c: room < 1e8 ? RED : INK, sub: `Hạn mức ${F(c.han_muc_cong_no_vnd)} · đang nợ ${F(c.cong_no_hien_tai_vnd)} · ${c.dieu_khoan_thanh_toan}` }; }) },
        { name: 'Tồn kho', rows: e.inventory.map(i => ({ id: i.sku, name: i.ten_san_pham, val: `${i.ton_kho} ${i.don_vi}`, c: i.ton_kho < i.diem_dat_hang_lai ? RED : INK, sub: `Điểm đặt hàng lại ${i.diem_dat_hang_lai} · ${i.kho}` })) },
        { name: 'Nhà cung cấp', rows: e.vendors.map(v => ({ id: v.vendor_id, name: v.ten_ncc, val: v.co_hoa_don_gtgt ? 'có GTGT' : 'không GTGT', c: v.co_hoa_don_gtgt ? INK : RED, sub: `Đánh giá giao hàng ${v.danh_gia_giao_hang} · ${v.dieu_khoan_cong_no}` })) },
        { name: 'Đơn mua hàng (PO)', rows: e.purchase_orders.map(p => ({ id: p.po_id, name: `${p.vendor_id} · ${p.so_luong} × ${p.sku}`, val: F(p.thanh_tien_vnd), c: p.trang_thai === 'tu_choi' ? RED : p.trang_thai === 'da_duyet' ? GREEN : AMBER, sub: `Trạng thái ${p.trang_thai} · tạo ${p.ngay_tao}` })), empty: 'Chưa có đơn mua hàng nào.' }
      ];
    },

    get uchips() {
      return ['all', ...(this.usage?.orders || [])].map(k => ({ k, label: k === 'all' ? 'Tất cả' : k, bg: this.uf === k ? INK : '#fff', fg: this.uf === k ? '#fff' : INK }));
    },
    get urows() {
      return (this.usage?.records || []).map((r, i) => ({
        i: i + 1, t: r.thoi_diem.slice(11, 19), order: r.order_id, agent: r.agent, model: r.model.replace('claude-', ''),
        inp: num(r.input_tokens), out: num(r.output_tokens), lat: r.latency_s.toFixed(1), cost: money(r.cost_usd), purpose: r.purpose,
        ac: r.agent === 'orchestrator' ? RC.llm.main : INK
      }));
    },
    get groups() {
      const g = this.usage?.groups || {};
      return [['Theo agent', 'agent'], ['Theo đơn hàng', 'order_id'], ['Theo model', 'model']].map(([name, key]) => ({
        name, rows: (g[key] || []).map(t => ({ k: t.name, a: `${t.calls} lượt`, b: `${num(t.input)} vào · ${num(t.output)} ra`, c: `${t.latency.toFixed(1)}s`, d: money(t.cost) }))
      }));
    },
    get jsonl() {
      return (this.usage?.records || []).map(r => JSON.stringify(r)).join('\n');
    },

    get agentCards() {
      return this.agents.map(a => ({
        id: a.id, role: a.role, goal: a.goal, model: a.model.replace('anthropic:', ''),
        tools: a.scoped_tools.length ? a.scoped_tools.join(', ') : '(không có)',
        read: a.memory_namespaces.read.join(', '), write: a.memory_namespaces.write.join(', '),
        thr: a.approval_threshold_vnd ? `${F(a.approval_threshold_vnd)} (${a.approval_threshold_vnd.toLocaleString('vi-VN')} VND)` : 'Không áp dụng',
        iter: `${a.max_iter} · ${a.allow_delegation ? 'có' : 'không'}`, owner: a.owner, cons: a.constraints
      }));
    }
  }));
});
