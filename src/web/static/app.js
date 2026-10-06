/* 生日提醒管理台的少量交互。
   原则：禁用 JS 时表单仍然完全可用 —— 这里只做增强，不接管提交。 */

/* 删除确认。低频工具里删除不可逆，加一道确认。 */
document.querySelectorAll('form.js-delete').forEach(function (form) {
  form.addEventListener('submit', function (event) {
    var name = form.getAttribute('data-name') || '这条收件人';
    if (!window.confirm('删除 ' + name + '？这会立刻写入 config.yml。')) {
      event.preventDefault();
    }
  });
});

/* 测试发送确认。这是真实发送，会真的发出邮件，必须让使用者先确认。 */
document.querySelectorAll('form.js-test-send').forEach(function (form) {
  form.addEventListener('submit', function (event) {
    var name = form.getAttribute('data-name') || '这条收件人';
    if (!window.confirm('现在就把 ' + name + ' 真实发出去？\n（会真的发送，不是预览）')) {
      event.preventDefault();
    }
  });
});

/* 密钥字段：默认只显示打码值，点"更换"才清空成可输入状态。
   禁用 JS 时输入框本身可用，只是少了一键清空的便利。 */
(function () {
  var input = document.querySelector('[data-secret-input]');
  var changeBtn = document.querySelector('[data-secret-change]');
  var mask = document.querySelector('[data-secret-mask]');
  if (!input || !changeBtn) {
    return;
  }

  changeBtn.addEventListener('click', function () {
    if (mask) {
      mask.hidden = true;
    }
    changeBtn.hidden = true;
    input.value = '';
    input.focus();
  });
})();

/* 阳历生日：即时校验 + 农历预览。
   服务端渲染时已给出初始状态；这里只在输入变化后刷新，节流 220ms。 */
(function () {
  var input = document.querySelector('[data-lunar-preview]');
  var output = document.querySelector('[data-lunar-output]');
  var status = document.querySelector('[data-solar-status]');
  if (!input || !output) {
    return;
  }

  var placeholder = '填好身份证出生年月日后，这里会显示下次农历生日与下次阳历生日。';

  var timer = null;
  var lastQuery = null;
  var controller = null;

  function setStatus(state, text) {
    if (!status) {
      return;
    }
    status.setAttribute('data-state', state);
    var textEl = status.querySelector('.input-status__text');
    if (textEl) {
      textEl.textContent = text || '';
    }
  }

  function renderNote(parts) {
    output.textContent = '';
    for (var i = 0; i < parts.length; i += 1) {
      var part = parts[i];
      if (part.strong) {
        var strong = document.createElement('strong');
        strong.textContent = part.text;
        output.appendChild(strong);
      } else {
        output.appendChild(document.createTextNode(part.text));
      }
    }
  }

  function showPlaceholder() {
    output.textContent = '';
    var span = document.createElement('span');
    span.className = 'lunar-note__placeholder';
    span.textContent = placeholder;
    output.appendChild(span);
    setStatus('empty', '');
  }

  function refresh() {
    var value = input.value.trim();

    if (!value) {
      lastQuery = null;
      showPlaceholder();
      return;
    }
    if (value === lastQuery) {
      return;
    }
    lastQuery = value;

    // 取消上一个仍在飞行中的请求，避免旧结果覆盖新输入。
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();

    fetch('/api/lunar?solar=' + encodeURIComponent(value), {
      headers: { 'Accept': 'application/json' },
      signal: controller.signal
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        // 用户可能在请求返回前又改了输入，丢弃过期结果。
        if (input.value.trim() !== value) {
          return;
        }
        if (data.ok) {
          setStatus('ok', '有效');
          output.setAttribute('data-state', 'ok');
          // 两个不同的日期：农历生日落在阳历哪天，以及阳历生日的下一次
          var parts = [{ text: '下次农历生日：' }, { text: data.display, strong: true }];
          if (data.next_solar) {
            parts.push({ text: '（' + data.next_solar + '）' });
          }
          if (data.next_solar_birthday) {
            parts.push({ text: '　下次阳历生日：' });
            parts.push({ text: data.next_solar_birthday, strong: true });
          }
          renderNote(parts);
        } else {
          // 阳历不合理时：不显示农历，只在这里说明问题。
          var hint = data.hint || '身份证出生年月日不合理，请检查格式和日期';
          setStatus('bad', '不合理');
          output.setAttribute('data-state', 'bad');
          renderNote([{ text: hint }]);
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          return;
        }
        showPlaceholder();
      });
  }

  /* 生日一律按 8 位数字填（身份证上的写法）。
     这里只拦"明显不是数字"的输入，不强行补零也不做日期校验 ——
     校验交给服务端，本地纠正太多反而会把人搞糊涂。
     粘贴 "1990-01-20" 这种带连字符的写法也接受，自动去掉分隔符。 */
  function digitsOnly(text) {
    return String(text || '').replace(/[^0-9]/g, '').slice(0, 8);
  }

  input.addEventListener('input', function () {
    var cleaned = digitsOnly(input.value);
    if (cleaned !== input.value) {
      var pos = input.selectionStart;
      var removed = input.value.length - cleaned.length;
      input.value = cleaned;
      // 保持光标位置：删掉的分隔符都在光标左边时才回退
      if (pos !== null) {
        try {
          input.setSelectionRange(Math.max(0, pos - removed), Math.max(0, pos - removed));
        } catch (e) { /* 某些浏览器对 text 输入框限制 setSelectionRange */ }
      }
    }
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(refresh, 220);
  });

  // 编辑页已有初始值时立即刷新一次，保证与后端口径一致。
  if (input.value.trim()) {
    refresh();
  }
})();

/* 时间轴的批量选择与筛选。
   禁用 JS 时勾选框仍然可用（能选中），批量按钮用 form="bulk-form" 关联，
   提交时把勾选索引写进隐藏字段 —— 所以禁用 JS 也能批量提交。 */
(function () {
  var form = document.getElementById('bulk-form');
  var bar = document.getElementById('bulkbar');
  if (!form || !bar) {
    return;
  }

  // 表格不在 form 里（HTML 禁止嵌套 form），行内测试发送/删除才能独立提交。
  var table = document.querySelector('table.tbl');
  var tbody = table ? table.querySelector('tbody') : null;
  var countEl = document.getElementById('bulk-count');
  var allBox = document.getElementById('bulk-all');
  var clearBtn = document.getElementById('bulk-clear');
  var emptyNote = document.getElementById('filter-empty');
  var indicesInput = document.getElementById('bulk-indices');

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll('tr.row'));
  }

  function boxes() {
    return rows().map(function (tr) { return tr.querySelector('.bulk-check'); });
  }

  /* 整行可点：复选框只有十几像素，点起来费劲。
     点行内任何空白处都切换选中状态。 */
  tbody.addEventListener('click', function (event) {
    // 行内的按钮/链接/输入框有自己的作用，点它们不能顺带选中整行 ——
    // 否则点「删除」会先把这一行勾上，很吓人。
    if (event.target.closest('a, button, input, label, select, textarea')) {
      return;
    }
    var tr = event.target.closest('tr.row');
    if (!tr || tr.hidden) {
      return;
    }
    var box = tr.querySelector('.bulk-check');
    if (!box) {
      return;
    }
    box.checked = !box.checked;
    refresh();
  });

  function refresh() {
    var all = boxes();
    var picked = all.filter(function (b) { return b.checked; });
    countEl.textContent = String(picked.length);
    // 一条都没选时收起操作条：它浮在页面底部，空着只会挡内容
    bar.hidden = picked.length === 0;

    // 勾选的索引写进隐藏字段：操作条按钮 form="bulk-form"，提交时带上它。
    if (indicesInput) {
      indicesInput.value = picked.map(function (b) { return b.value; }).join(',');
    }

    rows().forEach(function (tr) {
      var box = tr.querySelector('.bulk-check');
      // 用 class 而不是 :has()，兼容性更稳；也便于样式统一控制
      tr.classList.toggle('row--picked', !!(box && box.checked));
    });

    if (allBox) {
      var visible = all.filter(function (b) { return !b.closest('tr').hidden; });
      var pickedVisible = visible.filter(function (b) { return b.checked; });
      allBox.checked = visible.length > 0 && pickedVisible.length === visible.length;
      allBox.indeterminate = pickedVisible.length > 0 && pickedVisible.length < visible.length;
    }
  }

  /* ---------- 按受众筛选 ---------- */

  var currentFilter = 'all';

  function applyFilter(name) {
    currentFilter = name;
    var shown = 0;
    rows().forEach(function (tr) {
      var match = name === 'all' || tr.getAttribute('data-audience') === name;
      tr.hidden = !match;
      if (match) { shown += 1; }
    });
    if (emptyNote) { emptyNote.hidden = shown !== 0 || rows().length === 0; }

    // 筛选后**取消不可见的勾选**：否则会误改到看不见的记录。
    boxes().forEach(function (b) {
      if (b.closest('tr').hidden) { b.checked = false; }
    });

    document.querySelectorAll('.filterbtn').forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(btn.getAttribute('data-filter') === name));
    });
    refresh();
  }

  function refreshCounts() {
    var all = rows();
    var n = { all: all.length, group: 0, self: 0 };
    all.forEach(function (tr) {
      var a = tr.getAttribute('data-audience');
      if (a === 'group') { n.group += 1; } else { n.self += 1; }
    });
    Object.keys(n).forEach(function (k) {
      var el = document.querySelector('[data-count="' + k + '"]');
      if (el) { el.textContent = n[k] ? '(' + n[k] + ')' : ''; }
    });
  }

  document.querySelectorAll('.filterbtn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      applyFilter(btn.getAttribute('data-filter'));
    });
  });

  /* ---------- 勾选框与全选 ---------- */

  boxes().forEach(function (b) { b.addEventListener('change', refresh); });

  if (allBox) {
    allBox.addEventListener('change', function () {
      // 只全选**当前可见**的行：筛选状态下"全选"若选上隐藏的行，会误改数据
      boxes().forEach(function (b) {
        if (!b.closest('tr').hidden) { b.checked = allBox.checked; }
      });
      refresh();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      boxes().forEach(function (b) { b.checked = false; });
      refresh();
    });
  }

  // 改成「团体」的后果不可撤销，提交前确认一次
  form.querySelectorAll('[data-confirm]').forEach(function (btn) {
    btn.addEventListener('click', function (event) {
      if (!window.confirm(btn.getAttribute('data-confirm'))) {
        event.preventDefault();
      }
    });
  });

  refreshCounts();
  refresh();
})();

/* 提醒预览：邮件 iframe 按内容自适应高度。
   写死高度会让内容短时留一大块空白（邮件正文只有称呼+一两句+祝福），
   而内容长时又要滚动 —— 两种都不好。
   禁用 JS 时退回 CSS 里的固定高度，仍可滚动查看。 */
(function () {
  function fit(frame) {
    try {
      var doc = frame.contentDocument;
      if (!doc || !doc.body) { return; }
      // 加一点内边距，避免最后一行贴着边框
      var h = doc.body.scrollHeight + 2;
      if (h > 0) { frame.style.height = h + 'px'; }
    } catch (e) { /* 跨域或未就绪，保持 CSS 高度 */ }
  }

  function init() {
    // defer 脚本执行时 iframe 可能还没解析出来（querySelectorAll 返回空），
    // 所以这里必须**连"找不到 iframe"也重试** —— 只在找到后重试的话，
    // 一次空转就永远没机会了，表现成"高度始终是 CSS 兜底值"。
    var tries = 0;
    (function attempt() {
      var frames = document.querySelectorAll('iframe.mail-frame');
      if (!frames.length) {
        if (tries++ < 40) { requestAnimationFrame(attempt); }
        return;
      }
      Array.prototype.forEach.call(frames, function (frame) {
        if (frame.dataset.fitted === '1') { return; }
        frame.addEventListener('load', function () { fit(frame); });
        fit(frame);
        // srcdoc 的 iframe 可能在脚本执行前就解析完了（错过 load 事件），
        // 此时 contentDocument.body 未必就绪，读到的 scrollHeight 会是 0。
        // 用 rAF 少量重试兜住这个竞态。
        var inner = 0;
        (function retryFit() {
          fit(frame);
          if (frame.style.height) { frame.dataset.fitted = '1'; return; }
          if (inner++ < 20) { requestAnimationFrame(retryFit); }
        })();
      });
    })();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
