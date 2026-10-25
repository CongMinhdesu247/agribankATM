const API_URL = '/api';
let resolveModal, loginModal;
let barChartInstance = null, pieChartInstance = null;
let allTransactions = [], currentTransactionPage = 1, totalTransactions = 0;
let currentAuditPage = 1, totalAuditLogs = 0, currentRunsPage = 1, totalRuns = 0;
let currentRunId = '', currentRunPage = 1, currentRunCount = 0;
let currentUserRole = '';
let transactionFilterTimer = null;
const TRANSACTIONS_PER_PAGE = 100;

async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    const response = await fetch(url, {
        ...options, headers, cache: options.cache || 'no-store',
        credentials: options.credentials || 'same-origin'
    });
    if (response.status === 401 && sessionStorage.getItem('agribank_user')) {
        sessionStorage.removeItem('agribank_user');
        sessionStorage.removeItem('agribank_role');
        loginModal?.show();
    }
    return response;
}

document.addEventListener('DOMContentLoaded', () => {
    resolveModal = new bootstrap.Modal(document.getElementById('resolveModal'));
    loginModal = new bootstrap.Modal(document.getElementById('loginModal'));
    
    checkAuth();
});

async function checkAuth() {
    const response = await apiFetch(`${API_URL}/me`);
    if (!response.ok) {
        sessionStorage.removeItem('agribank_user');
        sessionStorage.removeItem('agribank_role');
        currentUserRole = '';
        applyRoleVisibility();
        loginModal.show();
        return;
    }
    const user = await response.json();
    currentUserRole = user.role;
    sessionStorage.setItem('agribank_user', user.username);
    sessionStorage.setItem('agribank_role', user.role);
    applyRoleVisibility();
    document.getElementById('current-user-display').textContent = user.username;
    await loadData();
}

async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    try {
        const response = await fetch(`${API_URL}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
            cache: 'no-store',
            credentials: 'same-origin'
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Đăng nhập thất bại.');
        sessionStorage.setItem('agribank_user', result.username);
        sessionStorage.setItem('agribank_role', result.role);
        currentUserRole = result.role;
        applyRoleVisibility();
        document.getElementById('current-user-display').textContent = result.username;
        document.getElementById('login-password').value = '';
        loginModal.hide();
        await loadData();
    } catch (error) {
        alert(error.message);
    }
}

async function logout() {
    try {
        const response = await apiFetch(`${API_URL}/logout`, { method: 'POST' });
        if (!response.ok && response.status !== 401) throw new Error('Logout failed. Please try again.');
    } catch (error) {
        alert(error.message);
        return;
    }
    sessionStorage.removeItem('agribank_user');
    sessionStorage.removeItem('agribank_role');
    location.reload();
}

function applyRoleVisibility() {
    document.getElementById('users-nav-item').classList.toggle('d-none', currentUserRole !== 'admin');
    document.getElementById('restore-initial-button').classList.toggle('d-none', currentUserRole !== 'admin');
    document.querySelector('[data-bs-target="#uploadModal"]').classList.toggle('d-none', currentUserRole === 'approver');
}

async function loadData() {
    const resStats = await apiFetch(`${API_URL}/stats`);
    if (!resStats.ok) throw new Error('Không tải được thống kê giao dịch.');
    const dataStats = await resStats.json();
    const stats = dataStats.data;
    
    const matched = stats.MATCHED || 0;
    const amtMismatch = stats.AMOUNT_MISMATCH || 0;
    const sttMismatch = stats.STATUS_MISMATCH || 0;
    const missCore = stats.MISSING_IN_CORE || 0;
    const missPartner = stats.MISSING_IN_PARTNER || 0;
    const missingTotal = missCore + missPartner;

    document.getElementById('stat-matched').innerText = matched.toLocaleString();
    document.getElementById('stat-amount-mismatch').innerText = amtMismatch.toLocaleString();
    document.getElementById('stat-status-mismatch').innerText = sttMismatch.toLocaleString();
    document.getElementById('stat-missing').innerText = missingTotal.toLocaleString();

    renderCharts(matched, amtMismatch, sttMismatch, missCore, missPartner);

    await populateStatusFilters();
    await loadTransactionPage(1);
}

async function populateStatusFilters() {
    const response = await apiFetch(`${API_URL}/anomaly-options`);
    if (!response.ok) throw new Error('Không tải được danh sách trạng thái.');
    const options = await response.json();
    [
        { id: 'filter-core-status', values: options.core_statuses || [] },
        { id: 'filter-partner-status', values: options.partner_statuses || [] }
    ].forEach(({ id, values }) => {
        const select = document.getElementById(id), previous = select.value;
        select.replaceChildren(new Option('Tất cả', ''), new Option('Không có dữ liệu', '__MISSING__'));
        values.forEach(status => select.add(new Option(status, status)));
        if ([...select.options].some(option => option.value === previous)) select.value = previous;
    });
}

function applyTransactionFilters() {
    clearTimeout(transactionFilterTimer);
    transactionFilterTimer = setTimeout(() => loadTransactionPage(1).catch(error => alert(error.message)), 250);
}

async function loadTransactionPage(page) {
    currentTransactionPage = page;
    const params = new URLSearchParams({
        page: String(page), page_size: String(TRANSACTIONS_PER_PAGE),
        trans_id: document.getElementById('filter-trans-id').value.trim(),
        recon_status: document.getElementById('filter-recon-status').value,
        core_status: document.getElementById('filter-core-status').value,
        partner_status: document.getElementById('filter-partner-status').value
    });
    const response = await apiFetch(`${API_URL}/anomalies?${params}`);
    if (!response.ok) throw new Error('Không tải được danh sách giao dịch cần xử lý.');
    const data = await response.json();
    allTransactions = data.data || [];
    totalTransactions = data.count || 0;
    renderTransactionsPage();
}

function clearTransactionFilters() {
    ['filter-trans-id', 'filter-recon-status', 'filter-core-status', 'filter-partner-status']
        .forEach(id => { document.getElementById(id).value = ''; });
    applyTransactionFilters();
}

function changeTransactionPage(offset) {
    const pageCount = Math.max(1, Math.ceil(totalTransactions / TRANSACTIONS_PER_PAGE));
    loadTransactionPage(Math.min(pageCount, Math.max(1, currentTransactionPage + offset)))
        .catch(error => alert(error.message));
}

function appendEmptyRow(tbody, columnCount, message) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = columnCount;
    cell.className = 'empty-state';
    cell.textContent = message;
    row.appendChild(cell);
    tbody.appendChild(row);
}

function renderTransactionsPage() {
    const tbody = document.getElementById('anomalies-table-body');
    tbody.replaceChildren();
    const start = (currentTransactionPage - 1) * TRANSACTIONS_PER_PAGE;
    const pageItems = allTransactions;

    if (!pageItems.length) appendEmptyRow(tbody, 7, 'Không có giao dịch nào khớp với bộ lọc.');

    pageItems.forEach(item => {
        const tr = document.createElement('tr');
        const addCell = (value, className = '') => {
            const td = document.createElement('td');
            td.textContent = value;
            if (className) td.className = className;
            tr.appendChild(td);
            return td;
        };
        addCell(item.trans_id, 'fw-bold');
        addCell(item.core_amount == null ? 'N/A' : `${Number(item.core_amount).toLocaleString()} ₫`);
        addCell(item.partner_amount == null ? 'N/A' : `${Number(item.partner_amount).toLocaleString()} ₫`);

        const addStatus = value => {
            const td = document.createElement('td');
            const badge = document.createElement('span');
            badge.className = `badge bg-${value === 'SUCCESS' ? 'success' : value === 'N/A' ? 'secondary' : 'warning'}`;
            badge.textContent = value || 'N/A';
            td.appendChild(badge);
            tr.appendChild(td);
        };
        addStatus(item.core_status || 'N/A');
        addStatus(item.partner_status || 'N/A');

        const statusCell = document.createElement('td');
        const statusBadge = document.createElement('span');
        statusBadge.className = 'badge bg-danger';
        statusBadge.textContent = item.recon_status;
        statusCell.appendChild(statusBadge);
        tr.appendChild(statusCell);

        const actionCell = addCell('', 'text-center');
        if (['admin', 'operator'].includes(currentUserRole)) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm btn-danger';
            button.innerHTML = '<i class="bi bi-tools me-1"></i> Xử lý';
            button.addEventListener('click', () => openResolveModal(item.trans_id));
            actionCell.appendChild(button);
        }
        tbody.appendChild(tr);
    });

    const pageCount = Math.max(1, Math.ceil(totalTransactions / TRANSACTIONS_PER_PAGE));
    document.getElementById('transaction-count').textContent = `Hiển thị ${totalTransactions ? start + 1 : 0}–${Math.min(start + pageItems.length, totalTransactions)} / ${totalTransactions.toLocaleString()} giao dịch cần xử lý`;
    document.getElementById('transactions-page').textContent = `Trang ${currentTransactionPage} / ${pageCount}`;
    document.getElementById('transactions-prev').disabled = currentTransactionPage <= 1;
    document.getElementById('transactions-next').disabled = currentTransactionPage >= pageCount;
}

async function restoreInitialData() {
    if (!confirm('Khôi phục bộ dữ liệu ban đầu? Dữ liệu đối soát hiện tại sẽ bị thay thế.')) return;
    const button = document.getElementById('restore-initial-button');
    if (button) button.disabled = true;
    try {
        const response = await apiFetch(`${API_URL}/restore-baseline`, { method: 'POST' });
        const result = await response.json();
        if (!response.ok || result.status !== 'success') {
            throw new Error(result.detail || result.message || 'Không khôi phục được danh sách ban đầu.');
        }
        clearTransactionFilters();
        await loadData();
    } catch (error) {
        alert(`Lỗi khôi phục dữ liệu: ${error.message}`);
    } finally {
        if (button) button.disabled = false;
    }
}

function renderCharts(matched, amtMismatch, sttMismatch, missCore, missPartner) {
    const ctxBar = document.getElementById('barChart').getContext('2d');
    if (barChartInstance) barChartInstance.destroy();
    barChartInstance = new Chart(ctxBar, {
        type: 'bar',
        data: {
            labels: ['Lệch Tiền', 'Lệch Trạng Thái', 'Mất Vết Core', 'Mất Vết Napas'],
            datasets: [{
                label: 'Số lượng giao dịch lệch',
                data: [amtMismatch, sttMismatch, missCore, missPartner],
                backgroundColor: ['#ffc107', '#dc3545', '#0d6efd', '#6c757d']
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true } }
        }
    });

    const ctxPie = document.getElementById('pieChart').getContext('2d');
    if (pieChartInstance) pieChartInstance.destroy();
    pieChartInstance = new Chart(ctxPie, {
        type: 'doughnut',
        data: {
            labels: ['Lệch Tiền', 'Lệch Trạng Thái', 'Mất Vết Core', 'Mất Vết Napas'],
            datasets: [{
                data: [amtMismatch, sttMismatch, missCore, missPartner],
                backgroundColor: ['#ffc107', '#dc3545', '#0d6efd', '#6c757d']
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'right' } }
        }
    });
}

async function loadAuditLogs() {
    const params = new URLSearchParams({
        page: String(currentAuditPage), page_size: '50',
        review_status: document.getElementById('audit-status-filter').value,
        performed_by: document.getElementById('audit-user-filter').value.trim(),
        date_from: document.getElementById('audit-date-from').value,
        date_to: document.getElementById('audit-date-to').value
    });
    const res = await apiFetch(`${API_URL}/audit-logs?${params}`);
    if (!res.ok) throw new Error('Không tải được nhật ký kiểm toán.');
    const data = await res.json();
    totalAuditLogs = data.count || 0;
    const tbody = document.getElementById('audit-table-body');
    tbody.replaceChildren();

    if (!data.data?.length) appendEmptyRow(tbody, 9, 'Chưa có nhật ký phù hợp với bộ lọc.');

    (data.data || []).forEach(log => {
        const tr = document.createElement('tr');
        const addCell = (value, className = '') => {
            const td = document.createElement('td');
            td.textContent = value == null ? '' : String(value);
            if (className) td.className = className;
            tr.appendChild(td);
            return td;
        };
        addCell(`#${log.log_id}`);
        addCell(log.trans_id, 'fw-bold');
        const actionCell = addCell('');
        const badge = document.createElement('span');
        badge.className = `badge bg-${log.action_type === 'FORCE_SUCCESS' ? 'success' : 'warning'}`;
        badge.textContent = log.action_type;
        actionCell.appendChild(badge);
        addCell(log.napas_ref_code, 'text-primary');
        addCell(log.reason);
        addCell(log.performed_by, 'fw-bold text-dark');
        addCell(log.created_at, 'text-muted small');
        const decisionCell = addCell('');
        const decisionBadge = document.createElement('span');
        decisionBadge.className = `badge ${log.review_status === 'APPROVED' ? 'bg-success' : log.review_status === 'PENDING' ? 'bg-warning text-dark' : 'bg-secondary'}`;
        decisionBadge.textContent = log.review_status || 'APPROVED';
        decisionCell.appendChild(decisionBadge);
        const approvalActionCell = addCell('');
        if (log.review_status === 'PENDING' && ['admin', 'approver'].includes(currentUserRole)
            && log.performed_by.toLowerCase() !== sessionStorage.getItem('agribank_user')?.toLowerCase()) {
            const approve = document.createElement('button');
            approve.type = 'button';
            approve.className = 'btn btn-success btn-sm me-1';
            approve.textContent = 'Duyệt';
            approve.addEventListener('click', () => decideReview(log.log_id, 'APPROVE'));
            const reject = document.createElement('button');
            reject.type = 'button';
            reject.className = 'btn btn-outline-danger btn-sm';
            reject.textContent = 'Từ chối';
            reject.addEventListener('click', () => decideReview(log.log_id, 'REJECT'));
            approvalActionCell.append(approve, reject);
        } else if (log.approved_by) {
            approvalActionCell.textContent = `Duyệt bởi ${log.approved_by}`;
            if (log.approval_comment) approvalActionCell.title = log.approval_comment;
        }
        tbody.appendChild(tr);
    });
    const pages = Math.max(1, Math.ceil(totalAuditLogs / 50));
    document.getElementById('audit-page').textContent = `Trang ${currentAuditPage} / ${pages} · ${totalAuditLogs.toLocaleString()} bản ghi`;
}

function changeAuditPage(offset) {
    const pages = Math.max(1, Math.ceil(totalAuditLogs / 50));
    currentAuditPage = Math.min(pages, Math.max(1, currentAuditPage + offset));
    loadAuditLogs().catch(error => alert(error.message));
}

function applyAuditFilters() {
    currentAuditPage = 1;
    loadAuditLogs().catch(error => alert(error.message));
}

function clearAuditFilters() {
    ['audit-status-filter', 'audit-user-filter', 'audit-date-from', 'audit-date-to']
        .forEach(id => { document.getElementById(id).value = ''; });
    applyAuditFilters();
}

async function decideReview(logId, decision) {
    const comment = prompt(decision === 'APPROVE' ? 'Ghi chú phê duyệt (không bắt buộc):' : 'Lý do từ chối:');
    if (comment === null) return;
    if (decision === 'REJECT' && !comment.trim()) { alert('Cần nhập lý do từ chối.'); return; }
    const response = await apiFetch(`${API_URL}/audit-logs/${logId}/decision`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, comment })
    });
    const result = await response.json();
    if (!response.ok) { alert(result.detail || 'Không lưu được quyết định.'); return; }
    await Promise.all([loadAuditLogs(), loadData()]);
}

async function loadRuns() {
    const response = await apiFetch(`${API_URL}/runs?page=${currentRunsPage}&page_size=25`);
    if (!response.ok) throw new Error('Không tải được lịch sử đối soát.');
    const result = await response.json();
    totalRuns = result.count || 0;
    const body = document.getElementById('runs-table-body');
    body.replaceChildren();
    if (!(result.data || []).length) appendEmptyRow(body, 8, 'Chưa có lần đối soát nào được ghi nhận.');
    (result.data || []).forEach(run => {
        const row = document.createElement('tr');
        [run.created_at, run.performed_by, run.core_filename, run.partner_filename,
            Number(run.transaction_count).toLocaleString(), Number(run.matched_count).toLocaleString(),
            Number(run.discrepancy_count).toLocaleString()].forEach(value => {
            const cell = document.createElement('td'); cell.textContent = value ?? ''; row.appendChild(cell);
        });
        const cell = document.createElement('td');
        const button = document.createElement('button'); button.className = 'btn btn-outline-primary btn-sm';
        button.textContent = 'Xem'; button.addEventListener('click', () => loadRun(run.run_id));
        cell.appendChild(button); row.appendChild(cell); body.appendChild(row);
    });
    const pages = Math.max(1, Math.ceil(totalRuns / 25));
    document.getElementById('runs-page').textContent = `Trang ${currentRunsPage} / ${pages} · ${totalRuns.toLocaleString()} lần đối soát`;
}

function changeRunsPage(offset) {
    const pages = Math.max(1, Math.ceil(totalRuns / 25));
    currentRunsPage = Math.min(pages, Math.max(1, currentRunsPage + offset));
    loadRuns().catch(error => alert(error.message));
}

async function loadRun(runId, page = 1) {
    currentRunId = runId; currentRunPage = page;
    const response = await apiFetch(`${API_URL}/runs/${encodeURIComponent(runId)}?page=${page}&page_size=100`);
    if (!response.ok) { alert('Không tải được chi tiết lần đối soát.'); return; }
    const result = await response.json(), detail = document.getElementById('run-detail');
    currentRunCount = Number(result.run.transaction_count);
    detail.classList.remove('d-none'); detail.replaceChildren();
    const title = document.createElement('h6');
    title.textContent = `Chi tiết lần đối soát ${result.run.run_id} · ${Number(result.run.transaction_count).toLocaleString()} giao dịch`;
    detail.appendChild(title);
    const table = document.createElement('table'); table.className = 'table table-sm table-striped';
    const head = document.createElement('thead'), headRow = document.createElement('tr');
    ['Mã giao dịch', 'Core', 'Napas', 'Trạng thái Core', 'Trạng thái Napas', 'Kết quả'].forEach(label => {
        const cell = document.createElement('th'); cell.textContent = label; headRow.appendChild(cell);
    });
    head.appendChild(headRow); table.appendChild(head);
    const body = document.createElement('tbody');
    (result.data || []).forEach(item => {
        const row = document.createElement('tr');
        [item.trans_id, item.core_amount, item.partner_amount, item.core_status, item.partner_status, item.recon_status].forEach(value => {
            const cell = document.createElement('td'); cell.textContent = value == null ? '—' : String(value); row.appendChild(cell);
        }); body.appendChild(row);
    });
    table.appendChild(body); detail.appendChild(table);
    const pageCount = Math.max(1, Math.ceil(currentRunCount / 100));
    const pager = document.createElement('div'); pager.className = 'd-flex justify-content-between align-items-center';
    const previous = document.createElement('button'); previous.className = 'btn btn-outline-secondary btn-sm'; previous.textContent = 'Trang trước';
    previous.disabled = currentRunPage <= 1; previous.addEventListener('click', () => loadRun(currentRunId, currentRunPage - 1));
    const pageLabel = document.createElement('span'); pageLabel.className = 'small text-muted'; pageLabel.textContent = `Trang ${currentRunPage} / ${pageCount}`;
    const next = document.createElement('button'); next.className = 'btn btn-outline-secondary btn-sm'; next.textContent = 'Trang sau';
    next.disabled = currentRunPage >= pageCount; next.addEventListener('click', () => loadRun(currentRunId, currentRunPage + 1));
    pager.append(previous, pageLabel, next); detail.appendChild(pager);
}

async function loadUsers() {
    const response = await apiFetch(`${API_URL}/users`);
    if (!response.ok) throw new Error('Không tải được danh sách tài khoản.');
    const result = await response.json(), body = document.getElementById('users-table-body');
    body.replaceChildren();
    if (!(result.data || []).length) appendEmptyRow(body, 6, 'Chưa có tài khoản nào.');
    (result.data || []).forEach(user => {
        const row = document.createElement('tr');
        [user.username, user.role, user.is_active ? 'Đang hoạt động' : 'Đã khóa', user.created_at, user.created_by].forEach(value => {
            const cell = document.createElement('td'); cell.textContent = value ?? ''; row.appendChild(cell);
        });
        const actionCell = document.createElement('td');
        if (user.username.toLowerCase() !== sessionStorage.getItem('agribank_user')?.toLowerCase()) {
            const button = document.createElement('button');
            button.className = `btn btn-sm ${user.is_active ? 'btn-outline-danger' : 'btn-outline-success'}`;
            button.textContent = user.is_active ? 'Khóa' : 'Mở khóa';
            button.addEventListener('click', () => setUserActive(user.username, !user.is_active));
            actionCell.appendChild(button);
        }
        row.appendChild(actionCell); body.appendChild(row);
    });
}

async function createUser(event) {
    event.preventDefault();
    const response = await apiFetch(`${API_URL}/users`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            username: document.getElementById('new-user-name').value.trim(),
            password: document.getElementById('new-user-password').value,
            role: document.getElementById('new-user-role').value
        })
    });
    const result = await response.json();
    if (!response.ok) { alert(result.detail || 'Không tạo được tài khoản.'); return; }
    event.target.reset(); await loadUsers();
}

async function setUserActive(username, isActive) {
    const response = await apiFetch(`${API_URL}/users/${encodeURIComponent(username)}/active`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: isActive })
    });
    const result = await response.json();
    if (!response.ok) { alert(result.detail || 'Không cập nhật được tài khoản.'); return; }
    await loadUsers();
}

async function downloadAuditReport() {
    try {
        const response = await apiFetch(`${API_URL}/reports/resolved.csv`);
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Không xuất được báo cáo.');
        }
        const blob = await response.blob();
        const downloadUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = downloadUrl;
        link.download = `bao_cao_tra_soat_${new Date().toISOString().replace(/[:.]/g, '-')}.csv`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
    } catch (error) {
        alert(error.message);
    }
}

function openResolveModal(transId) {
    document.getElementById('modal-trans-id').value = transId;
    document.getElementById('display-trans-id').value = transId;
    document.getElementById('modal-napas-ref').value = '';
    document.getElementById('modal-reason').value = '';
    resolveModal.show();
}
async function submitUploadLogs() {
    const coreFileInput = document.getElementById('coreFile');
    const partnerFileInput = document.getElementById('partnerFile');
    const alertBox = document.getElementById('uploadAlert');

    if (!coreFileInput.files[0] || !partnerFileInput.files[0]) {
        alert('Vui lòng chọn đủ 2 file Core Log và Partner Log!');
        return;
    }

    const formData = new FormData();
    formData.append('core_file', coreFileInput.files[0]);
    formData.append('partner_file', partnerFileInput.files[0]);

    if (coreFileInput.files[0].size > 10 * 1024 * 1024 || partnerFileInput.files[0].size > 10 * 1024 * 1024) {
        alert('Mỗi file CSV phải có dung lượng không quá 10 MB.');
        return;
    }

    alertBox.classList.remove('d-none');

    try {
        const previewResponse = await apiFetch('/api/upload-preview', { method: 'POST', body: formData });
        const preview = await previewResponse.json();
        if (!previewResponse.ok) throw new Error(preview.detail || 'Không kiểm tra được file.');
        formData.append('expected_run_id', preview.current_run_id || '');
        const breakdown = Object.entries(preview.by_status || {})
            .map(([status, count]) => `${status}: ${Number(count).toLocaleString()}`).join('\n');
        const confirmed = confirm(
            `Xem trước kết quả đối soát:\n` +
            `Core: ${coreFileInput.files[0].name}\nPartner: ${partnerFileInput.files[0].name}\n\n` +
            `Tổng giao dịch: ${Number(preview.transaction_count).toLocaleString()}\n` +
            `Khớp: ${Number(preview.matched_count).toLocaleString()}\n` +
            `Cần tra soát: ${Number(preview.discrepancy_count).toLocaleString()}\n` +
            `${breakdown ? `\nPhân loại:\n${breakdown}\n` : ''}\n` +
            `Nếu tiếp tục, dữ liệu hiện tại sẽ được thay bằng kết quả mới.`
        );
        if (!confirmed) return;
        const response = await apiFetch('/api/upload-and-reconcile', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();

        if (response.ok && result.status === 'success') {
            alert('Đối soát hoàn tất thành công!');
            if (result.warning) alert(result.warning);
            // Đóng modal
            const modalElement = document.getElementById('uploadModal');
            const modal = bootstrap.Modal.getInstance(modalElement);
            modal.hide();
            // Tải lại dữ liệu mới lên Dashboard
            clearTransactionFilters();
            await loadData();
        } else {
            throw new Error(result.detail || result.message || response.statusText);
        }
    } catch (error) {
        console.error('Lỗi upload:', error);
        alert(error.message || 'Lỗi kết nối máy chủ!');
    } finally {
        alertBox.classList.add('d-none');
    }
}
async function submitResolve() {
    const trans_id = document.getElementById('modal-trans-id').value;
    const action_type = document.getElementById('modal-action-type').value;
    const napas_ref_code = document.getElementById('modal-napas-ref').value;
    const reason = document.getElementById('modal-reason').value;

    if (!napas_ref_code.trim() || !reason.trim()) {
        alert('⚠️ Bắt buộc phải nhập Mã điện Napas và Lý do điều chỉnh!');
        return;
    }

    const res = await apiFetch(`${API_URL}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            trans_id,
            action_type,
            napas_ref_code,
            reason
        })
    });

    if (res.ok) {
        alert('Đã gửi yêu cầu xử lý để người có quyền phê duyệt xác nhận.');
        resolveModal.hide();
        loadData();
    } else {
        const err = await res.json();
        alert('❌ Lỗi: ' + err.detail);
    }
}

// Explicit callbacks keep inline JavaScript disabled by Content Security Policy.
const pageActions = {
    logout: event => logout(),
    loadAuditLogs: event => loadAuditLogs(),
    loadRuns: event => loadRuns(),
    loadUsers: event => loadUsers(),
    restoreInitialData: event => restoreInitialData(),
    applyTransactionFilters: event => applyTransactionFilters(),
    clearTransactionFilters: event => clearTransactionFilters(),
    previousTransactionPage: event => changeTransactionPage(-1),
    nextTransactionPage: event => changeTransactionPage(1),
    downloadAuditReport: event => downloadAuditReport(),
    applyAuditFilters: event => applyAuditFilters(),
    clearAuditFilters: event => clearAuditFilters(),
    previousAuditPage: event => changeAuditPage(-1),
    nextAuditPage: event => changeAuditPage(1),
    previousRunsPage: event => changeRunsPage(-1),
    nextRunsPage: event => changeRunsPage(1),
    createUser: event => createUser(event),
    handleLogin: event => handleLogin(event),
    submitResolve: event => submitResolve(),
    submitUploadLogs: event => submitUploadLogs()
};
for (const eventType of ['click', 'input', 'change', 'submit']) {
    document.addEventListener(eventType, event => {
const target = event.target.closest(`[data-${eventType}]`);
const callback = target && pageActions[target.getAttribute(`data-${eventType}`)];
if (callback) Promise.resolve(callback(event)).catch(error => alert(error.message));
    });
}
