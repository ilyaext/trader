import React, { useState, useEffect, useRef, useMemo } from 'react';
import axios from 'axios';
import {
    Activity, LayoutDashboard, History, Zap,
    X, ExternalLink, RefreshCw, Trash2,
    ChevronRight, AlertCircle, TrendingUp, TrendingDown,
    Monitor, Play, Pause, Download
} from 'lucide-react';

const API_BASE = '/api';
const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;

const App = () => {
    const [activeTab, setActiveTab] = useState('trading');
    const [health, setHealth] = useState({ status: 'offline', ib_connected: false });
    const [account, setAccount] = useState({ net_liquidation: 0, total_cash: 0, daily_pnl: 0, daily_pnl_pct: 0 });
    const [portfolio, setPortfolio] = useState([]);
    const [orders, setOrders] = useState([]);
    const [strategies, setStrategies] = useState([]);
    const [logs, setLogs] = useState([]);
    const [testPrice, setTestPrice] = useState(100);
    const [prices, setPrices] = useState({});
    const [wsConnected, setWsConnected] = useState(false);
    const [config, setConfig] = useState({ target_investment: 3000, stop_loss_pct: 3.0 });
    const [showingConfig, setShowingConfig] = useState(false);

    // Form states
    const [buyForm, setBuyForm] = useState({ ticker: '', qty: 1, sl: 0 });
    const [historyForm, setHistoryForm] = useState({
        ticker: 'SPY',
        start: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
        end: new Date().toISOString().split('T')[0],
        barSize: '1 min'
    });
    const [stratForm, setStratForm] = useState({ ticker: '', entry: '', sl: 0, qty: 1 });

    // Refs for persistent data
    const ws = useRef(null);

    // Initial Fetching
    const fetchAll = async () => {
        try {
            const [hRes, aRes, pRes, oRes, sRes, lRes] = await Promise.all([
                axios.get(`${API_BASE}/health`),
                axios.get(`${API_BASE}/account`),
                axios.get(`${API_BASE}/portfolio`),
                axios.get(`${API_BASE}/orders`),
                axios.get(`${API_BASE}/strategies`),
                axios.get(`${API_BASE}/logs`),
                axios.get(`${API_BASE}/config`)
            ]);
            setHealth(hRes.data);
            setAccount(aRes.data);
            setPortfolio(pRes.data);
            setOrders(oRes.data);
            setStrategies(sRes.data);
            setLogs(lRes.data);
            setConfig(cRes.data);
        } catch (err) {
            console.error("Fetch Error:", err);
        }
    };

    useEffect(() => {
        fetchAll();
        const pollHealth = setInterval(async () => {
            try {
                const hRes = await axios.get(`${API_BASE}/health`);
                setHealth(hRes.data);
            } catch (e) {
                setHealth({ status: 'offline', ib_connected: false });
            }
        }, 5000);
        return () => clearInterval(pollHealth);
    }, []);

    // WebSocket Connection
    useEffect(() => {
        const connectWS = () => {
            ws.current = new WebSocket(WS_URL);
            ws.current.onopen = () => setWsConnected(true);
            ws.current.onclose = () => {
                setWsConnected(false);
                setTimeout(connectWS, 3000); // Reconnect
            };
            ws.current.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.type === 'price') {
                    setPrices(prev => ({ ...prev, [msg.ticker]: msg.price }));
                } else if (msg.type === 'order_update' || msg.type === 'position_update') {
                    // Trigger a full re-fetch to update orders, portfolio, and account
                    fetchAll();
                } else if (msg.type === 'log') {
                    setLogs(prev => [msg.data, ...prev].slice(0, 50));
                }
            };
        };
        connectWS();
        return () => ws.current?.close();
    }, []);

    // Complex Logic: Process Orders (Merging Parent/Child)
    const processedOrders = useMemo(() => {
        if (!orders || !Array.isArray(orders) || orders.length === 0) return [];

        // Use map to create fresh objects to avoid state mutation
        const allOrders = orders.map(o => ({ ...o }));
        const parents = allOrders.filter(o => !o.parent_id || o.parent_id === 0);
        const children = allOrders.filter(o => o.parent_id && o.parent_id !== 0);

        parents.forEach(p => {
            const child = children.find(c => c.parent_id === p.id);
            if (child) {
                p.attached_stop_price = child.stop_price;
                p.attached_stop_qty = child.total_qty;
                p.attached_stop_action = child.action;
                p.attached_stop_status = child.status;
            }
        });

        const parentIds = parents.map(p => p.id);
        const orphans = children.filter(c => !parentIds.includes(c.parent_id));
        const combined = [...parents, ...orphans];

        const activeStatuses = ['Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending', 'Simulated', 'Inactive']; // Inactive can stay at top if it might turn active, but user said Active (working)
        // Let's stick to the "Actionable" ones
        const priorityStatuses = ['Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending', 'Simulated'];

        return combined.sort((a, b) => {
            const aPrio = priorityStatuses.includes(a.status) ? 0 : (a.status === 'Filled' ? 1 : 2);
            const bPrio = priorityStatuses.includes(b.status) ? 0 : (b.status === 'Filled' ? 1 : 2);
            if (aPrio !== bPrio) return aPrio - bPrio;
            // Newest first within same priority
            return b.time.localeCompare(a.time);
        });
    }, [orders]);

    // Actions
    const handleBuy = async () => {
        if (!buyForm.ticker) return;
        try {
            await axios.post(`${API_BASE}/order`, {
                ticker: buyForm.ticker.toUpperCase(),
                action: 'BUY',
                quantity: buyForm.qty,
                stop_loss: buyForm.sl > 0 ? buyForm.sl : null
            });
            fetchAll();
            setBuyForm({ ticker: '', qty: 1, sl: 0 });
        } catch (e) { alert(e.response?.data?.detail || "Order failed"); }
    };

    const closePosition = async (ticker) => {
        if (!window.confirm(`Confirm Market Close for ${ticker}?`)) return;
        await axios.post(`${API_BASE}/positions/close?ticker=${ticker}`);
        fetchAll();
    };

    const cancelOrder = async (orderId) => {
        if (!window.confirm("Are you sure you want to cancel this order?")) return;
        await axios.post(`${API_BASE}/orders/${orderId}/cancel`);
        fetchAll();
    };

    const deleteStrategy = async (id) => {
        if (!window.confirm("Are you sure you want to delete this alert?")) return;
        await axios.delete(`${API_BASE}/strategies/${id}`);
        fetchAll();
    };

    const toggleStrategyLive = async (id, currentVal) => {
        await axios.patch(`${API_BASE}/strategies/${id}`, { is_live: !currentVal });
        fetchAll();
    };

    const updateTestPrice = async () => {
        await axios.post(`${API_BASE}/test/price`, { ticker: 'TEST', price: Number(testPrice) });
        fetchAll();
    };

    const downloadHistory = async () => {
        try {
            const res = await axios.post(`${API_BASE}/history/download`, {
                ticker: historyForm.ticker.toUpperCase(),
                start_date: historyForm.start,
                end_date: historyForm.end,
                bar_size: historyForm.barSize
            });
            alert(`Success! Saved to ${res.data.file}`);
        } catch (e) { alert(e.response?.data?.detail || "Download failed"); }
    };

    const createStrategy = async (e) => {
        e.preventDefault();
        try {
            await axios.post(`${API_BASE}/strategies`, {
                ticker: stratForm.ticker.toUpperCase(),
                entry_price: Number(stratForm.entry),
                stop_loss: Number(stratForm.sl),
                quantity: Number(stratForm.qty)
            });
            fetchAll();
            setStratForm({ ticker: '', entry: '', sl: 0, qty: 1 });
        } catch (e) { alert(e.response?.data?.detail || "Failed to create strategy"); }
    };

    const updateConfig = async (newConfig) => {
        try {
            await axios.post(`${API_BASE}/config`, newConfig);
            setConfig(newConfig);
            setShowingConfig(false);
        } catch (e) { alert("Failed to update configuration"); }
    };

    const onEntryChange = (val) => {
        const entry = Number(val);
        const sl = entry * (1 - config.stop_loss_pct / 100);
        const qty = Math.floor(config.target_investment / entry);
        setStratForm({ ...stratForm, entry: val, sl: sl.toFixed(2), qty: qty || 1 });
    };

    // UI Helpers
    const fmtUSD = (v) => {
        if (v === undefined || v === null || isNaN(v)) return '$0.00';
        return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(v);
    };

    const fmtPct = (v) => {
        if (v === undefined || v === null || isNaN(v)) return '0.00%';
        return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    };

    const getStatusColor = (status) => {
        if (!status) return 'grey';
        const active = ['Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending', 'Simulated'];
        if (active.includes(status)) return 'orange';
        if (status === 'Filled') return 'green';
        return 'grey';
    };

    return (
        <div className="dashboard">
            {/* Sidebar */}
            <aside className="sidebar">
                <div className="logo">
                    <Zap size={28} className="blue" />
                    <span>Trader Bot</span>
                </div>

                <div className="sidebar-section">
                    <div className="section-title">Connectivity</div>
                    <div className="status-badge">
                        <div className={`dot ${health.status === 'ok' ? 'green' : 'red'}`}></div>
                        <span>Backend: {health.status === 'ok' ? 'Online' : 'Offline'}</span>
                    </div>
                    <div className="status-badge">
                        <div className={`dot ${health.ib_connected ? 'green' : 'red'}`}></div>
                        <span>IBKR: {health.ib_connected ? 'Connected' : 'Disconnected'}</span>
                    </div>
                    <div className={`status-badge ${wsConnected ? 'green' : 'orange'}`} style={{ fontSize: '0.7rem', marginTop: '4px' }}>
                        <RefreshCw size={10} className={wsConnected ? "" : "spin"} />
                        <span>WebSocket: {wsConnected ? "Active" : "Reconnecting..."}</span>
                    </div>
                </div>

                <div className="sidebar-section">
                    <div className="section-title">Account Summary</div>
                    <div className="account-metric">
                        <div className="metric-label">Net Liquidation</div>
                        <div className="metric-value">{fmtUSD(account?.net_liquidation || 0)}</div>
                    </div>
                    <div className="account-metric">
                        <div className="metric-label">Free Cash</div>
                        <div className="metric-value">{fmtUSD(account?.total_cash || 0)}</div>
                    </div>
                    <div className="account-metric">
                        <div className="metric-label">Daily P/L</div>
                        <div className={`metric-value mono ${account?.daily_pnl >= 0 ? 'green' : 'red'}`}>{fmtUSD(account?.daily_pnl || 0)}</div>
                        <div className={`metric-delta ${account?.daily_pnl_pct >= 0 ? 'green' : 'red'}`}>
                            {fmtPct(account?.daily_pnl_pct || 0)}
                        </div>
                    </div>
                </div>

                <div className="sidebar-section">
                    <div className="section-title">Settings</div>
                    <button className="btn-secondary" style={{ width: '100%', justifyContent: 'flex-start', gap: '8px' }} onClick={() => setShowingConfig(!showingConfig)}>
                        <RefreshCw size={14} /> Strat Config
                    </button>
                    {showingConfig && (
                        <div className="config-box" style={{ marginTop: '10px', padding: '10px', background: '#1c2128', borderRadius: '6px', border: '1px solid #30363d' }}>
                            <div className="form-group">
                                <label style={{ fontSize: '0.7rem' }}>Default Risk ($)</label>
                                <input type="number"
                                    value={config.target_investment}
                                    onChange={e => setConfig({ ...config, target_investment: Number(e.target.value) })}
                                    style={{ padding: '4px', fontSize: '0.8rem' }}
                                />
                            </div>
                            <div className="form-group">
                                <label style={{ fontSize: '0.7rem' }}>Default SL (%)</label>
                                <input type="number"
                                    value={config.stop_loss_pct}
                                    onChange={e => setConfig({ ...config, stop_loss_pct: Number(e.target.value) })}
                                    style={{ padding: '4px', fontSize: '0.8rem' }}
                                />
                            </div>
                            <button className="btn-primary" style={{ width: '100%', marginTop: '8px', padding: '4px' }} onClick={() => updateConfig(config)}>Save</button>
                        </div>
                    )}
                </div>

                <div className="sidebar-section mt-auto">
                    <div className="section-title">Test Lab</div>
                    <div className="form-group">
                        <label>TEST Price</label>
                        <div className="flex">
                            <input
                                type="number"
                                value={testPrice}
                                onChange={(e) => setTestPrice(e.target.value)}
                                step="0.5"
                                style={{ width: '90px' }}
                            />
                            <button className="btn-primary" onClick={updateTestPrice}>Set</button>
                        </div>
                    </div>
                </div>
            </aside>

            {/* Main Content */}
            <main className="main-content">
                <header className="tabs">
                    <div className={`tab ${activeTab === 'trading' ? 'active' : ''}`} onClick={() => setActiveTab('trading')}>Trading</div>
                    <div className={`tab ${activeTab === 'historical' ? 'active' : ''}`} onClick={() => setActiveTab('historical')}>Historical Data</div>
                    <div className={`tab ${activeTab === 'agent' ? 'active' : ''}`} onClick={() => setActiveTab('agent')}>Strategy Agent</div>
                </header>

                <div className="tab-panel">
                    {activeTab === 'trading' && (
                        <>
                            {/* Positions Card */}
                            <section className="card">
                                <div className="card-title">
                                    <div className="flex"><LayoutDashboard size={18} /> Current Positions</div>
                                </div>
                                <div className="table-container">
                                    <table>
                                        <thead>
                                            <tr>
                                                <th>Ticker</th>
                                                <th>CHG %</th>
                                                <th>P/L $</th>
                                                <th>Cur. Price</th>
                                                <th>Qty</th>
                                                <th>Avg Price</th>
                                                <th>Stop Price</th>
                                                <th>Risk</th>
                                                <th>Close</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {portfolio?.length > 0 ? portfolio.map(p => (
                                                <tr key={p.ticker}>
                                                    <td className="ticker-cell">{p.ticker}</td>
                                                    <td className={p.today_pnl_pct >= 0 ? 'green' : 'red'}>{fmtPct(p.today_pnl_pct)}</td>
                                                    <td className={`${p.today_pnl >= 0 ? 'green' : 'red'} mono`}>{fmtUSD(p.today_pnl)}</td>
                                                    <td className="mono">{fmtUSD(prices[p.ticker] || p.market_price)}</td>
                                                    <td>{p.quantity}</td>
                                                    <td className="mono">{fmtUSD(p.avg_cost)}</td>
                                                    <td className="mono">{p.stop_loss > 0 ? fmtUSD(p.stop_loss) : '-'}</td>
                                                    <td className="mono">{p.risk_amount !== 0 ? fmtUSD(p.risk_amount) : '-'}</td>
                                                    <button className="btn-icon" onClick={() => closePosition(p.ticker)}>
                                                        <Trash2 size={14} />
                                                    </button>
                                                </tr>
                                            )) : (
                                                <tr><td colSpan="9" style={{ textAlign: 'center', color: '#8b949e', padding: '2rem' }}>No open positions.</td></tr>
                                            )}
                                        </tbody>
                                    </table>
                                </div>
                            </section>

                            {/* Order Entry & Strategies */}
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
                                <section className="card">
                                    <div className="card-title"><Zap size={18} className="blue" /> Manual Buy Order</div>
                                    <div className="form-grid form-grid-compact">
                                        <div className="form-group">
                                            <label>Ticker</label>
                                            <input type="text" placeholder="e.g. AAPL" value={buyForm.ticker} onChange={e => setBuyForm({ ...buyForm, ticker: e.target.value.toUpperCase() })} />
                                        </div>
                                        <div className="form-group">
                                            <label>Qty</label>
                                            <input type="number" value={buyForm.qty} onChange={e => setBuyForm({ ...buyForm, qty: Number(e.target.value) })} min="1" />
                                        </div>
                                        <div className="form-group">
                                            <label>Stop Loss ($)</label>
                                            <input type="number" value={buyForm.sl} onChange={e => setBuyForm({ ...buyForm, sl: Number(e.target.value) })} step="0.01" />
                                        </div>
                                        <button className="btn-primary" onClick={handleBuy}>Place Order</button>
                                    </div>
                                </section>

                                <section className="card">
                                    <div className="card-title"><Activity size={18} className="blue" /> Create Alert</div>
                                    <form className="form-grid form-grid-compact" onSubmit={createStrategy}>
                                        <div className="form-group">
                                            <label>Ticker</label>
                                            <input type="text" value={stratForm.ticker} onChange={e => setStratForm({ ...stratForm, ticker: e.target.value.toUpperCase() })} required />
                                        </div>
                                        <div className="form-group">
                                            <label>Entry Alert ($)</label>
                                            <input type="number" value={stratForm.entry} onChange={e => onEntryChange(e.target.value)} step="0.01" required />
                                        </div>
                                        <div className="form-group">
                                            <label>Stop Loss ($)</label>
                                            <input type="number" value={stratForm.sl} onChange={e => setStratForm({ ...stratForm, sl: e.target.value })} step="0.01" />
                                        </div>
                                        <div className="form-group">
                                            <label>Quantity</label>
                                            <input type="number" value={stratForm.qty} onChange={e => setStratForm({ ...stratForm, qty: e.target.value })} />
                                        </div>
                                        <button className="btn-primary" type="submit">Set Alert</button>
                                    </form>
                                </section>
                            </div>

                            {/* Strategies Terminal */}
                            <section className="card">
                                <div className="card-title">📡 Alerts</div>
                                <div className="table-container">
                                    <table>
                                        <thead>
                                            <tr>
                                                <th>Ticker</th>
                                                <th>Mode</th>
                                                <th>Price</th>
                                                <th>Daily %</th>
                                                <th>Entry Alert</th>
                                                <th>Stop Loss</th>
                                                <th>Qty</th>
                                                <th>Action</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {strategies.filter(s => s.status === 'active').map(s => (
                                                <tr key={s.id}>
                                                    <td className="ticker-cell">{s.ticker}</td>
                                                    <td>
                                                        <button
                                                            className={`btn-icon ${s.is_live ? 'blue' : 'orange'}`}
                                                            onClick={() => toggleStrategyLive(s.id, s.is_live)}
                                                            title={s.is_live ? "Live (Auto)" : "Manual Injection"}
                                                        >
                                                            {s.is_live ? <Zap size={14} /> : <Monitor size={14} />}
                                                        </button>
                                                    </td>
                                                    <td className="mono">{fmtUSD(prices[s.ticker] || s.current_price)}</td>
                                                    <td className={s.daily_change_pct >= 0 ? 'green' : 'red'}>{s.daily_change_pct ? fmtPct(s.daily_change_pct) : '-'}</td>
                                                    <td className="mono">{fmtUSD(s.entry_price)}</td>
                                                    <td className="mono">{s.stop_loss ? fmtUSD(s.stop_loss) : '-'}</td>
                                                    <td>{s.quantity}</td>
                                                    <td>
                                                        <button className="btn-icon" onClick={() => deleteStrategy(s.id)}><Trash2 size={14} /></button>
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            </section>

                            {/* Orders Table - The 10 Column View */}
                            <section className="card">
                                <div className="card-title">📦 Active Orders</div>
                                <div className="table-container">
                                    <table>
                                        <thead>
                                            <tr>
                                                <th>Ticker</th>
                                                <th>Type</th>
                                                <th>Qty</th>
                                                <th>Limit</th>
                                                <th>Price</th>
                                                <th>SL Qty</th>
                                                <th>SL Val</th>
                                                <th>Status</th>
                                                <th>Time</th>
                                                <th>Cancel</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {processedOrders?.map(o => {
                                                const isFilled = o.status === 'Filled';
                                                const slStatus = o.attached_stop_status || '';
                                                const slActive = ['Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending', 'Simulated'].includes(slStatus);

                                                const tickerGrey = (isFilled && !slActive) || ['Cancelled', 'Inactive'].includes(o.status);
                                                const typeGrey = isFilled || ['Cancelled', 'Inactive'].includes(o.status);
                                                const slGrey = !slActive;

                                                return (
                                                    <tr key={o.id}>
                                                        <td className={`ticker-cell ${tickerGrey ? 'grey' : o.status === 'Simulated' ? 'blue' : 'orange'}`}>{o.ticker}</td>
                                                        <td className={typeGrey ? 'grey' : o.action === 'BUY' ? 'green' : 'red'}>{o.action} {o.type}</td>
                                                        <td className={typeGrey ? 'grey' : ''}>{o.total_qty}</td>
                                                        <td className={`mono ${typeGrey ? 'grey' : ''}`}>{o.price > 0 ? fmtUSD(o.price) : 'MKT'}</td>
                                                        <td className={`mono ${isFilled && !slActive ? 'grey' : ''}`}>{fmtUSD(prices[o.ticker] || o.current_or_filled_price)}</td>

                                                        {/* Stop Loss Columns */}
                                                        <td className={slGrey ? 'grey' : o.attached_stop_action === 'SELL' ? 'red' : 'green'}>
                                                            {o.attached_stop_price > 0 ? o.attached_stop_qty : '-'}
                                                        </td>
                                                        <td className={`mono ${slGrey ? 'grey' : o.attached_stop_action === 'SELL' ? 'red' : 'green'}`} style={{ fontWeight: 600 }}>
                                                            {o.attached_stop_price > 0 ? fmtUSD(o.attached_stop_price) : '-'}
                                                        </td>
                                                        <td className={isFilled && !slActive ? 'grey' : ''}>{o.status}</td>
                                                        <td className={isFilled && !slActive ? 'grey' : ''} style={{ fontSize: '0.8rem' }}>{o.time}</td>
                                                        <td>
                                                            {(o.status === 'Submitted' || o.status === 'PreSubmitted' || o.status === 'Simulated') ? (
                                                                <button className="btn-icon" onClick={() => cancelOrder(o.id)}><Trash2 size={14} /></button>
                                                            ) : null}
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            </section>

                            {/* Strategy Logs Terminal */}
                            <section className="card" style={{ flexGrow: 1 }}>
                                <div className="card-title">📜 System Terminal</div>
                                <div style={{
                                    background: '#010409',
                                    padding: '1rem',
                                    borderRadius: '6px',
                                    fontFamily: 'JetBrains Mono',
                                    fontSize: '0.8rem',
                                    height: '200px',
                                    overflowY: 'auto',
                                    border: '1px solid #30363d'
                                }}>
                                    {logs.map((log, i) => (
                                        <div key={i} style={{ marginBottom: '4px', color: log.includes('✅') || log.includes('🚀') ? '#3fb950' : log.includes('❌') ? '#f85149' : '#8b949e' }}>
                                            {log}
                                        </div>
                                    ))}
                                </div>
                            </section>
                        </>
                    )}

                    {activeTab === 'historical' && (
                        <section className="card">
                            <div className="card-title">Download Historical Data</div>
                            <div className="form-grid form-grid-compact">
                                <div className="form-group">
                                    <label>Ticker</label>
                                    <input type="text" value={historyForm.ticker} onChange={e => setHistoryForm({ ...historyForm, ticker: e.target.value.toUpperCase() })} />
                                </div>
                                <div className="form-group">
                                    <label>Start Date</label>
                                    <input type="date" value={historyForm.start} onChange={e => setHistoryForm({ ...historyForm, start: e.target.value })} />
                                </div>
                                <div className="form-group">
                                    <label>End Date</label>
                                    <input type="date" value={historyForm.end} onChange={e => setHistoryForm({ ...historyForm, end: e.target.value })} />
                                </div>
                                <div className="form-group">
                                    <label>Bar Size</label>
                                    <select value={historyForm.barSize} onChange={e => setHistoryForm({ ...historyForm, barSize: e.target.value })}>
                                        <option>1 min</option>
                                        <option>5 mins</option>
                                        <option>1 hour</option>
                                        <option>1 day</option>
                                    </select>
                                </div>
                                <button className="btn-primary flex" onClick={downloadHistory}>
                                    <Download size={18} /> Download
                                </button>
                            </div>
                        </section>
                    )}

                    {activeTab === 'agent' && (
                        <div style={{ textAlign: 'center', padding: '5rem', color: '#8b949e' }}>
                            <Activity size={48} style={{ marginBottom: '1rem', opacity: 0.5 }} />
                            <h3>Strategy Agent Dashboard</h3>
                            <p>Additional advanced analysis features coming soon.</p>
                        </div>
                    )}
                </div>
            </main>

            <style>{`
        .spin { animation: spin 2s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
      `}</style>
        </div>
    );
};

export default App;
