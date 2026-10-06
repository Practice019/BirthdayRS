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

/* 时间轴的批量选择：勾选任意一条后浮现操作条，用于批量改受众。
   禁用 JS 时勾选框仍然可用（能选中、能提交），只是少了计数与"全选"的联动 ——
   所以这里只做增强，不做拦截。 */
(function () {
  var form = document.getElementById('bulk-form');
  var bar = document.getElementById('bulkbar');
  if (!form || !bar) {
    return;
  }

  var boxes = Array.prototype.slice.call(form.querySelectorAll('.bulk-check'));
  var countEl = document.getElementById('bulk-count');
  var allBox = document.getElementById('bulk-all');
  var clearBtn = document.getElementById('bulk-clear');

  function refresh() {
    var picked = boxes.filter(function (b) { return b.checked; });
    countEl.textContent = String(picked.length);
    // 一条都没选时收起操作条：它浮在页面底部，空着只会挡内容
    bar.hidden = picked.length === 0;

    if (allBox) {
      allBox.checked = picked.length === boxes.length && boxes.length > 0;
      // indeterminate 表示"选了一部分"，这是勾选框的标准表意
      allBox.indeterminate = picked.length > 0 && picked.length < boxes.length;
    }
  }

  boxes.forEach(function (b) { b.addEventListener('change', refresh); });

  if (allBox) {
    allBox.addEventListener('change', function () {
      boxes.forEach(function (b) { b.checked = allBox.checked; });
      refresh();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      boxes.forEach(function (b) { b.checked = false; });
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

  refresh();
})();
