(function () {
  // Only run inside an iframe (TAMALE embedding)
  if (window.self === window.top) return;

  var TAMALE_ORIGIN = 'https://tamaleapp.com';

  function getWorkflowIdFromRow(el) {
    // Walk up to find <tr>, then read first <td> (ID column)
    var row = el.closest && el.closest('tr');
    if (!row) return null;
    var firstCell = row.querySelector('td');
    if (!firstCell) return null;
    var id = firstCell.textContent.trim();
    return /^\d+$/.test(id) ? id : null;
  }

  function isEditButton(el) {
    // Walk up to find a BUTTON whose text content is "Edit"
    var btn = el.closest && el.closest('button');
    if (!btn) return null;
    var txt = (btn.textContent || '').trim();
    if (txt !== 'Edit') return null;
    return btn;
  }

  document.addEventListener('click', function (e) {
    var btn = isEditButton(e.target);
    if (!btn) return;

    var wfId = getWorkflowIdFromRow(btn);
    if (!wfId) return;

    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();

    try {
      window.parent.postMessage(
        { type: 'TAMALE_EDIT_AGENT', workflow_id: wfId },
        TAMALE_ORIGIN
      );
    } catch (err) {
      console.error('[TAMALE Interceptor] postMessage failed:', err);
    }
  }, true); // capture phase — runs BEFORE Next.js router
})();
