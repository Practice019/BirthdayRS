/* ==========================================================================
   生日提醒 · 桌面应用前端
   与 web 版的区别：所有后端调用走 window.pywebview.api（进程内），无 HTTP。
   ========================================================================== */

(function () {
  'use strict';

  var api = null;          // pywebviewready 之后可用
  var currentView = 'timeline';
  var editingIndex = null; // null = 新增

  /* ---------- 基础工具 ---------- */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function setFlash(kind, message) {
    var box = $('#flash');
    box.className = 'flash hidden';
    if (!message) { return; }
    box.className = 'flash flash--' + kind;
    box.textContent = message;
  }

  function requireApi() {
    if (!api) {
      setFlash('err', '后端还没就绪，请稍候再试。');
      return false;
    }
    return true;
  }

  /* 统一处理 api 返回：ok 为假时弹提示并返回 null。 */
  function handleResult(result, onOk) {
    if (!result) {
      setFlash('err', '没有收到后端响应。');
      return null;
    }
    if (!result.ok) {
      setFlash('err', result.error || '操作失败');
      return null;
    }
    if (result.notice) { setFlash('ok', result.notice); }
    if (onOk) { onOk(result); }
    return result;
  }

  /* 网络/异常包装：js_api 调用抛错时也要有可见反馈 */
  function call(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    var fn = window.pywebview.api[method];
    if (!fn) {
      setFlash('err', '后端缺少方法：' + method);
      return Promise.resolve(null);
    }
    return fn.apply(window.pywebview.api, args).catch(function (err) {
      console.error(method, err);
      setFlash('err', '调用 ' + method + ' 失败：' + (err && err.message ? err.message : err));
      return null;
    });
  }

  /* ---------- 视图切换 ---------- */

  var VIEWS = ['timeline', 'form', 'settings'];

  function show(view) {
    currentView = view;
    VIEWS.forEach(function (name) {
      var section = document.getElementById('view-' + name);
      if (section) { section.classList.toggle('hidden', name !== view); }
    });
    $$('.nav__link').forEach(function (btn) {
      btn.classList.toggle('nav__link--current', btn.getAttribute('data-view') === view);
    });
    $('.main').scrollTop = 0;
    setFlash('ok', '');
  }

  /* ---------- 时间轴 ---------- */

  function renderTimeline(data) {
    var summary = data.summary || {};
    var meta = $('#meta');
    meta.textContent = '';

    meta.appendChild(el('span', 'meta__label', '通知渠道'));
    var types = summary.types || [];
    if (!types.length) {
      meta.appendChild(el('span', 'meta__item meta__item--warn', '未配置'));
    } else {
      types.forEach(function (t) {
        var label = (t === 'email' || t === 'resend') ? '邮件'
                  : (t === 'serverchan' ? '微信推送' : (t === 'wxpusher' ? '手机推送' : t));
        meta.appendChild(el('span', 'chip', label));
      });
    }

    // 三种发信方式任一生效都算"邮件已配置"，不能只看 SMTP
    if (summary.resend_configured) {
      var target = summary.resend_receive_email || '未填接收邮箱';
      var who = el('span', 'meta__item');
      who.appendChild(el('span', null, (summary.resend_from_name || 'onboarding') + ' 发往 '));
      who.appendChild(el('span', 'mono', target));
      meta.appendChild(who);
    } else if (summary.smtp_configured) {
      meta.appendChild(el('span', 'meta__item',
        (summary.smtp_username || '') + ' 通过 ' + (summary.smtp_host || '') + ':' + (summary.smtp_port || '')));
    } else {
      var warn = el('span', 'meta__item meta__item--warn');
      warn.appendChild(document.createTextNode('邮件未配置，'));
      var link = el('button', 'btn btn--sm', '去设置');
      link.type = 'button';
      link.addEventListener('click', function () { openSettings(); });
      warn.appendChild(link);
      meta.appendChild(warn);
    }

    meta.appendChild(el('span', 'meta__sep'));
    meta.appendChild(el('span', 'meta__label', '默认提前'));
    meta.appendChild(el('span', 'meta__item', (summary.default_reminder_days || 0) + ' 天'));

    $('#timeline-sub').textContent = '共 ' + data.total + ' 人。'
      + (data.trigger_count ? '其中 ' + data.trigger_count + ' 人会在提醒窗口内发送。' : '');

    var tbody = $('#timeline-body');
    tbody.textContent = '';

    if (!data.recipients.length) {
      $('#timeline-table').classList.add('hidden');
      $('#timeline-empty').classList.remove('hidden');
      return;
    }
    $('#timeline-table').classList.remove('hidden');
    $('#timeline-empty').classList.add('hidden');

    data.recipients.forEach(function (r) {
      tbody.appendChild(buildRow(r));
    });

    // 重新渲染后旧的勾选已经失效，重置操作条避免"选了 3 条"其实是过去的行
    refreshFilterCounts();
    refreshBulkBar();
  }

  function buildRow(r) {
    var tr = el('tr', 'row row--' + r.status);
    tr.setAttribute('data-audience', r.audience === 'group' ? 'group' : 'self');

    // 选择框（批量改受众用）
    var tdCheck = el('td', 'tbl__check');
    tdCheck.setAttribute('data-label', '选择');
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'bulk-check';
    box.value = String(r.index);
    box.setAttribute('aria-label', '选择 ' + r.name);
    box.addEventListener('change', refreshBulkBar);
    tdCheck.appendChild(box);
    tr.appendChild(tdCheck);

    // 姓名 + 备注
    var tdName = el('td', null);
    tdName.setAttribute('data-label', '姓名');
    tdName.appendChild(el('span', 'name', r.name));
    // 广播给团体是不可撤销的，必须一眼看得出这条会发出去
    if (r.audience === 'group') {
      tdName.appendChild(el('span', 'chip chip--sm chip--group', '团体'));
    }
    if (r.note) { tdName.appendChild(el('span', 'sub', r.note)); }
    tr.appendChild(tdName);

    // 生日（固定信息）
    var tdBirth = el('td', null);
    tdBirth.setAttribute('data-label', '生日');
    if (r.is_lunar_only) {
      tdBirth.appendChild(el('span', 'mono', r.solar_birthday || ''));
      tdBirth.appendChild(el('span', 'sub sub--warn',
        '只有农历生日（旧格式）。编辑这条补上阳历生日后会自动换算'));
    } else if (r.solar_invalid) {
      tdBirth.appendChild(el('span', 'mono', r.solar_birthday || '未填'));
      tdBirth.appendChild(el('span', 'sub sub--warn', '身份证出生年月日不合理，请修正'));
    } else {
      tdBirth.appendChild(el('span', 'mono', r.solar_birthday || ''));
      if (r.lunar_text) {
        tdBirth.appendChild(el('span', 'sub', '农历 ' + r.lunar_text));
        if (r.lunar_mismatch) {
          tdBirth.appendChild(el('span', 'sub sub--warn', '配置里的农历值与推算不符，已按阳历计算'));
        }
      }
    }
    tr.appendChild(tdBirth);

    // 下一次（取更近的那个，并标注是哪种日历）
    var tdNext = el('td', null);
    tdNext.setAttribute('data-label', '下一次');
    if (r.next_birthday) {
      tdNext.appendChild(el('span', 'mono', r.next_birthday));
      var line = el('span', 'sub');
      if (r.next_kind) {
        line.appendChild(el('span', 'chip chip--sm', r.next_kind));
        line.appendChild(document.createTextNode(' '));
      }
      line.appendChild(document.createTextNode(
        '星期' + r.week_name + (r.age ? ' · 满 ' + r.age + ' 岁' : '')));
      tdNext.appendChild(line);

      // 另一种日历的日期也给出来，否则会疑惑"那天怎么不算"
      if (r.next_kind === '阳历' && r.lunar_next_solar) {
        tdNext.appendChild(el('span', 'sub muted', '农历生日另在 ' + r.lunar_next_solar));
      } else if (r.next_kind === '农历' && r.solar_birthday) {
        tdNext.appendChild(el('span', 'sub muted', '阳历生日另在 ' + r.solar_birthday));
      }
    } else {
      tdNext.appendChild(el('span', 'sub', '无法识别'));
    }
    tr.appendChild(tdNext);

    // 倒计时
    var tdCount = el('td', null);
    tdCount.setAttribute('data-label', '倒计时');
    tdCount.appendChild(el('span', 'badge badge--' + r.status, r.status_text));
    tr.appendChild(tdCount);

    // 提醒窗口
    var tdWindow = el('td', null);
    tdWindow.setAttribute('data-label', '提醒窗口');
    tdWindow.appendChild(el('span', 'mono', r.reminder_days));
    tdWindow.appendChild(document.createTextNode(' 天'));
    if (r.reminder_days_inherited) {
      tdWindow.appendChild(el('span', 'sub muted', '默认'));
    }
    tr.appendChild(tdWindow);

    // 操作
    var tdAct = el('td', 'row__actions');
    tdAct.setAttribute('data-label', '操作');
    tdAct.appendChild(actionBtn('预览', 'btn btn--sm', function () { openPreview(r.index); }));
    tdAct.appendChild(actionBtn('测试发送', 'btn btn--sm', function () { testSend(r.index, r.name); }));
    tdAct.appendChild(actionBtn('编辑', 'btn btn--sm', function () { openForm(r.index); }));
    tdAct.appendChild(actionBtn('删除', 'btn btn--sm btn--danger', function () { removeRecipient(r.index, r.name); }));
    tr.appendChild(tdAct);

    return tr;
  }

  function actionBtn(label, className, handler) {
    var btn = el('button', className, label);
    btn.type = 'button';
    btn.addEventListener('click', handler);
    return btn;
  }

  function loadTimeline(keepFilter) {
    if (!requireApi()) { return Promise.resolve(); }
    return call('get_timeline').then(function (data) {
      if (!data) { return; }
      if (!data.ok) {
        setFlash('err', data.error || '加载失败');
        return;
      }
      renderTimeline(data);
      // 重新渲染后**总是**沿用当前筛选：删除、批量改受众这类操作之后，
      // 使用者的视角不该突然跳回全部。首次加载时 currentFilter 就是 'all'。
      applyFilter(keepFilter || currentFilter);
    });
  }

  /* ---------- 新增 / 编辑 ---------- */

  function openForm(index) {
    editingIndex = (typeof index === 'number') ? index : null;
    var isEdit = editingIndex !== null;

    $('#form-title').textContent = isEdit ? '编辑收件人' : '添加收件人';
    $('#form-sub').textContent = isEdit
      ? '修改后会直接写入 config.yml，原文件的注释会保留。'
      : '填姓名和身份证上的出生年月日就行，农历生日会自动算出来。';

    var form = $('#recipient-form');
    form.reset();
    $('#form-error').classList.add('hidden');
    $('#form-error').textContent = '';
    clearFieldErrors();

    if (!isEdit) {
      // 留空 = 用设置里的默认值，不把当前默认值固化到这个人身上。
      $('#f-reminder_days').value = '';
      // 默认"只发给我自己"：广播出去的信息收不回来，想广播必须显式选
      $('#f-audience').value = 'self';
      renderLunarNote('empty', null);
      show('form');
      $('#f-name').focus();
      return;
    }

    call('get_recipient', editingIndex).then(function (data) {
      if (!data) { return; }
      if (!data.ok) {
        setFlash('err', data.error || '读取失败');
        return;
      }
      var v = data.values || {};
      $('#f-name').value = v.name || '';
      $('#f-solar').value = v.solar_birthday || '';
      // 没单独设置过的人留空展示，而不是回填一个具体数字
      $('#f-reminder_days').value = (v.reminder_days !== undefined && v.reminder_days !== null) ? v.reminder_days : '';
      // 旧记录没有 audience，语义上就是"只发给我自己"
      $('#f-audience').value = v.audience || 'self';
      $('#f-note').value = v.note || '';
      if (v.solar_birthday) {
        runSolarCheck(v.solar_birthday);
      } else {
        renderLunarNote('empty', null);
      }
      show('form');
    });
  }

  function clearFieldErrors() {
    $$('#recipient-form .field').forEach(function (f) { f.classList.remove('field--bad'); });
    $$('#recipient-form .field__err').forEach(function (e) { e.remove(); });
  }

  function showFieldErrors(errors) {
    clearFieldErrors();
    if (!errors) { return; }
    Object.keys(errors).forEach(function (field) {
      if (field === '__all__') {
        $('#form-error').className = 'field__err';
        $('#form-error').textContent = errors[field];
        return;
      }
      var input = document.getElementById('f-' + field);
      if (!input) { return; }
      var wrapper = input.closest('.field');
      if (wrapper) { wrapper.classList.add('field--bad'); }
      var err = el('p', 'field__err', errors[field]);
      if (input.parentNode && input.parentNode.classList.contains('input-group')) {
        input.parentNode.parentNode.appendChild(err);
      } else {
        input.parentNode.appendChild(err);
      }
    });
  }

  function collectForm() {
    return {
      name: $('#f-name').value,
      solar_birthday: $('#f-solar').value,
      reminder_days: $('#f-reminder_days').value,
      audience: $('#f-audience').value,
      note: $('#f-note').value
    };
  }

  function submitForm(event) {
    event.preventDefault();
    if (!requireApi()) { return; }

    var values = collectForm();
    var promise = editingIndex === null
      ? call('save_recipient', values, null)
      : call('save_recipient', values, editingIndex);

    promise.then(function (res) {
      if (!res) { return; }
      if (!res.ok) {
        if (res.errors && Object.keys(res.errors).length) {
          showFieldErrors(res.errors);
        }
        setFlash('err', res.error || '保存失败');
        return;
      }
      setFlash('ok', res.notice || '已保存');
      show('timeline');
      loadTimeline();
    });
  }

  /* ---------- 身份证日期的即时校验 ---------- */

  function renderLunarNote(state, data) {
    var note = $('#lunar-note');
    note.textContent = '';
    note.setAttribute('data-state', state || 'empty');

    if (state === 'ok' && data) {
      note.appendChild(document.createTextNode('下次农历生日：'));
      note.appendChild(el('strong', null, data.lunar_display));
      if (data.lunar_next_solar) {
        note.appendChild(document.createTextNode('（' + data.lunar_next_solar + '）'));
      }
      if (data.next_solar_birthday) {
        note.appendChild(el('span', null, '　下次阳历生日：'));
        note.appendChild(el('strong', null, data.next_solar_birthday));
      }
    } else if (state === 'bad') {
      note.textContent = (data && data.error) || '身份证出生年月日不合理，请检查格式和日期。';
    } else {
      note.textContent = '填好身份证出生年月日后，这里会显示下次农历生日与下次阳历生日。';
    }

    var status = $('#solar-status');
    status.setAttribute('data-state', state || 'empty');
    $('.input-status__text', status).textContent =
      state === 'ok' ? '有效' : (state === 'bad' ? '不合理' : '');
  }

  var solarTimer = null;
  var lastSolar = null;

  function runSolarCheck(value) {
    if (!api) { return; }
    if (value === lastSolar) { return; }
    lastSolar = value;
    call('validate_solar', value).then(function (res) {
      if (!res) { return; }
      if (res.state === 'empty') { renderLunarNote('empty', null); return; }
      if (!res.ok) { renderLunarNote('bad', res); return; }
      renderLunarNote('ok', res);
    });
  }

  /* 生日按 8 位数字填（身份证上的写法）。
     只拦非数字字符，粘贴带连字符的写法也接受；日期是否合法交给后端判断。 */
  function digitsOnly(text) {
    return String(text || '').replace(/[^0-9]/g, '').slice(0, 8);
  }

  function onSolarInput() {
    var field = $('#f-solar');
    var cleaned = digitsOnly(field.value);
    if (cleaned !== field.value) {
      field.value = cleaned;
    }
    var value = cleaned.trim();
    if (solarTimer) { clearTimeout(solarTimer); }
    if (!value) { lastSolar = null; renderLunarNote('empty', null); return; }
    solarTimer = setTimeout(function () { runSolarCheck(value); }, 220);
  }

  /* ---------- 批量改受众 + 筛选 ---------- */

  function bulkRows() {
    return Array.prototype.slice.call($$('#timeline-body tr.row'));
  }

  function bulkBoxes() {
    return bulkRows().map(function (tr) { return tr.querySelector('.bulk-check'); });
  }

  function refreshBulkBar() {
    var boxes = bulkBoxes();
    var picked = boxes.filter(function (b) { return b && b.checked; });
    var bar = $('#bulkbar');
    var count = $('#bulk-count');
    if (!bar) { return; }
    if (count) { count.textContent = String(picked.length); }
    // 没选时不显示：它贴底浮着，空着只会挡内容
    bar.classList.toggle('hidden', picked.length === 0);

    bulkRows().forEach(function (tr) {
      var box = tr.querySelector('.bulk-check');
      tr.classList.toggle('row--picked', !!(box && box.checked));
    });

    var all = $('#bulk-all');
    if (all) {
      // 只统计当前可见的行：筛选状态下"全选"不该选上隐藏的行
      var visible = boxes.filter(function (b) { return b && !b.closest('tr').hidden; });
      var pickedVisible = visible.filter(function (b) { return b.checked; });
      all.checked = visible.length > 0 && pickedVisible.length === visible.length;
      all.indeterminate = pickedVisible.length > 0 && pickedVisible.length < visible.length;
    }
  }

  function applyFilter(name) {
    currentFilter = name;
    var shown = 0;
    bulkRows().forEach(function (tr) {
      var match = name === 'all' || tr.getAttribute('data-audience') === name;
      tr.classList.toggle('hidden', !match);
      if (match) { shown += 1; }
    });

    // 筛选后取消不可见的勾选，否则会误改到看不见的记录
    bulkBoxes().forEach(function (b) {
      if (b && b.closest('tr').hidden) { b.checked = false; }
    });

    $$('.filterbtn').forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(btn.getAttribute('data-filter') === name));
    });
    refreshBulkBar();
  }

  var currentFilter = 'all';

  function refreshFilterCounts() {
    var n = { all: 0, group: 0, self: 0 };
    bulkRows().forEach(function (tr) {
      n.all += 1;
      if (tr.getAttribute('data-audience') === 'group') { n.group += 1; } else { n.self += 1; }
    });
    Object.keys(n).forEach(function (k) {
      var el = document.querySelector('[data-count="' + k + '"]');
      if (el) { el.textContent = n[k] ? '(' + n[k] + ')' : ''; }
    });
  }

  function applyAudience(audience) {
    if (!requireApi()) { return; }
    var picked = bulkBoxes().filter(function (b) { return b && b.checked; });
    if (!picked.length) { return; }

    if (audience === 'group') {
      var ok = window.confirm(
        '设为「团体」后，这 ' + picked.length + ' 人过生日时会广播给推送应用里的所有人，'
        + '消息发出去收不回来。确定吗？');
      if (!ok) { return; }
    }

    call('set_audience_bulk', picked.map(function (b) { return Number(b.value); }), audience)
      .then(function (res) {
        if (!res) { return; }
        if (!res.ok) {
          setFlash('err', res.error || '保存失败');
          return;
        }
        setFlash('ok', res.notice || '已保存');
        // 重新拉列表后筛选要沿用，否则改完受众视角会突然跳回全部
        loadTimeline(currentFilter);
      });
  }

  function initBulkBar() {
    // 整行可点：复选框太小，点起来费劲
    var tbody = $('#timeline-body');
    if (tbody) {
      tbody.addEventListener('click', function (event) {
        // 行内按钮有自己的作用，点它们不能顺带选中整行
        if (event.target.closest('a, button, input, label, select, textarea')) { return; }
        var tr = event.target.closest('tr.row');
        if (!tr || tr.classList.contains('hidden')) { return; }
        var box = tr.querySelector('.bulk-check');
        if (!box) { return; }
        box.checked = !box.checked;
        refreshBulkBar();
      });
    }

    var all = $('#bulk-all');
    if (all) {
      all.addEventListener('change', function () {
        bulkBoxes().forEach(function (b) {
          if (b && !b.closest('tr').hidden) { b.checked = all.checked; }
        });
        refreshBulkBar();
      });
    }

    var clear = $('#bulk-clear');
    if (clear) {
      clear.addEventListener('click', function () {
        bulkBoxes().forEach(function (b) { if (b) { b.checked = false; } });
        refreshBulkBar();
      });
    }

    $$('.filterbtn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyFilter(btn.getAttribute('data-filter'));
      });
    });

    var selfBtn = $('#bulk-self');
    if (selfBtn) { selfBtn.addEventListener('click', function () { applyAudience('self'); }); }
    var groupBtn = $('#bulk-group');
    if (groupBtn) { groupBtn.addEventListener('click', function () { applyAudience('group'); }); }
  }

  /* ---------- 预览 ---------- */

  function openPreview(index) {
    if (!requireApi()) { return; }
    call('preview_recipient', index).then(function (res) {
      if (!res) { return; }
      if (!res.ok) {
        setFlash('err', res.error || '无法预览');
        return;
      }

      $('#preview-title').textContent = '提醒预览 · ' + res.name;
      var bits = [];
      if (res.days_until === 0) { bits.push('今天发送'); }
      else if (res.days_until !== null && res.days_until !== undefined) { bits.push('还有 ' + res.days_until + ' 天'); }
      if (res.age) { bits.push('满 ' + res.age + ' 岁'); }
      $('#preview-sub').textContent = '这是按今天的日期算出的下一次生日，实际发送时的内容。'
        + bits.join('，') + '。预览不会真的发出通知。';

      var mailBox = $('#preview-mail');
      mailBox.textContent = '';
      if (res.email_error) {
        mailBox.appendChild(el('p', 'flash flash--err', res.email_error));
      } else if (res.email_html) {
        var frame = el('iframe', 'mail-frame');
        frame.title = '邮件预览';
        frame.setAttribute('srcdoc', res.email_html);
        mailBox.appendChild(frame);
      } else {
        mailBox.appendChild(el('p', 'muted', '没有可预览的邮件内容。'));
      }

      $('#preview-wechat').textContent = res.push_text || '（没能生成推送文案）';
      show('preview');
    });
  }

  /* ---------- 测试发送 ---------- */

  function testSend(index, name) {
    if (!requireApi()) { return; }
    if (!window.confirm('现在就把 ' + name + ' 的提醒真实发出去？\n（会真的发送，不是预览）')) {
      return;
    }
    setFlash('ok', '正在发送…');
    call('test_send', index).then(function (res) {
      handleResult(res);
    });
  }

  /* ---------- 删除 ---------- */

  function removeRecipient(index, name) {
    if (!requireApi()) { return; }
    if (!window.confirm('删除 ' + name + '？这会立刻写入 config.yml。')) { return; }
    call('delete_recipient', index).then(function (res) {
      var done = handleResult(res);
      if (done) { loadTimeline(); }
    });
  }

  /* ---------- 设置 ---------- */

  function openSettings() {
    if (!requireApi()) { return; }
    call('get_settings').then(function (data) {
      if (!data) { return; }
      if (!data.ok) {
        setFlash('err', data.error || '读取设置失败');
        return;
      }
      var v = data.values || {};
      $('#s-receive').value = v.resend_receive_email || '';
      $('#s-from-name').value = v.resend_from_name || '';
      $('#s-from-email').value = v.resend_from_email || '';
      $('#s-key').value = '';
      $('#s-key-mask').textContent = v.resend_key_masked || '';
      $('#s-key-status').classList.toggle('hidden', !v.has_resend_key);
      $('#s-clear-key-wrap').classList.toggle('hidden', !v.has_resend_key);
      $('#s-clear-key').checked = false;

      // 手机推送
      $('#s-spt').value = '';
      $('#s-spt-mask').textContent = v.wxpusher_spt_masked || '';
      $('#s-spt-status').classList.toggle('hidden', !v.has_wxpusher_spt);
      $('#s-clear-spt-wrap').classList.toggle('hidden', !v.has_wxpusher_spt);
      $('#s-clear-spt').checked = false;

      $('#s-app-token').value = '';
      $('#s-app-token-mask').textContent = v.wxpusher_app_token_masked || '';
      $('#s-app-token-status').classList.toggle('hidden', !v.has_wxpusher_app_token);
      $('#s-clear-app-token-wrap').classList.toggle('hidden', !v.has_wxpusher_app_token);
      $('#s-clear-app-token').checked = false;
      // UID 不是密钥，原样回填方便核对
      $('#s-self-uid').value = v.wxpusher_self_uid || '';

      // 启用哪些渠道
      var enabled = v.enabled_types || [];
      $('#s-enable-resend').checked = enabled.indexOf('resend') !== -1;
      $('#s-enable-wxpusher').checked = enabled.indexOf('wxpusher') !== -1;

      var select = $('#s-days');
      select.textContent = '';
      (data.days_options || []).forEach(function (opt) {
        var option = el('option', null, opt.label);
        option.value = String(opt.value);
        if (Number(opt.value) === Number(v.default_reminder_days)) { option.selected = true; }
        select.appendChild(option);
      });

      var summary = data.summary || {};
      $('#s-summary').textContent = '';
      var parts = [];
      if (enabled.indexOf('resend') !== -1) {
        parts.push(summary.resend_receive_email || summary.default_receive_email || '邮箱（还没填）');
      }
      if (enabled.indexOf('wxpusher') !== -1) { parts.push('手机'); }
      $('#s-summary').appendChild(document.createTextNode(
        '保存后可以发一条测试提醒，确认能送到 ' + (parts.length ? parts.join(' 和 ') : '已启用的渠道')
        + '。这会真实发送，会消耗渠道额度。'));

      clearSettingsErrors();
      show('settings');
    });
  }

  function clearSettingsErrors() {
    $$('#settings-form .field').forEach(function (f) { f.classList.remove('field--bad'); });
    $$('#settings-form .field__err').forEach(function (e) { e.remove(); });
    [$('#s-key-err'), $('#s-spt-err'), $('#s-app-token-err'), $('#s-self-uid-err'),
     $('#s-receive-err'), $('#s-enable-err')].forEach(function (e) {
      if (e) { e.classList.add('hidden'); e.textContent = ''; }
    });
  }

  function showSettingsErrors(errors) {
    clearSettingsErrors();
    // 字段名 → 错误显示位置。用固定节点而不是「插到输入框后面」：
    // 表单现在是分组结构，插到输入框后面容易落到错误的 fieldset 里。
    var fixed = {
      api_key: 's-key-err',
      spt: 's-spt-err',
      app_token: 's-app-token-err',
      self_uid: 's-self-uid-err',
      default_receive_email: 's-receive-err',
      enabled_types: 's-enable-err'
    };
    var byInput = { from_name: 's-from-name', from_email: 's-from-email' };

    Object.keys(errors || {}).forEach(function (field) {
      var box = document.getElementById(fixed[field] || '');
      if (box) {
        box.textContent = errors[field];
        box.classList.remove('hidden');
        return;
      }
      var input = byInput[field] ? document.getElementById(byInput[field]) : null;
      if (!input) { return; }
      var wrapper = input.closest('.field');
      if (wrapper) { wrapper.classList.add('field--bad'); }
      input.parentNode.appendChild(el('p', 'field__err', errors[field]));
    });
  }

  function submitSettings(event) {
    event.preventDefault();
    if (!requireApi()) { return; }

    var values = {
      api_key: $('#s-key').value,
      spt: $('#s-spt').value,
      app_token: $('#s-app-token').value,
      self_uid: $('#s-self-uid').value,
      default_receive_email: $('#s-receive').value,
      from_name: $('#s-from-name').value,
      from_email: $('#s-from-email').value,
      default_reminder_days: $('#s-days').value,
      enabled_types: []
    };
    if ($('#s-enable-resend').checked) { values.enabled_types.push('resend'); }
    if ($('#s-enable-wxpusher').checked) { values.enabled_types.push('wxpusher'); }
    if ($('#s-clear-key').checked) { values.clear_api_key = true; }
    if ($('#s-clear-spt').checked) { values.clear_spt = true; }
    if ($('#s-clear-app-token').checked) { values.clear_app_token = true; }

    call('save_settings', values).then(function (res) {
      if (!res) { return; }
      if (!res.ok) {
        if (res.errors) { showSettingsErrors(res.errors); }
        setFlash('err', res.error || '保存失败');
        return;
      }
      setFlash('ok', res.notice || '设置已保存');
      openSettingsKeepNotice(res.notice);
    });
  }

  /* 重新拉一次设置以刷新打码值，但保留刚才的成功提示 */
  function openSettingsKeepNotice(notice) {
    call('get_settings').then(function (data) {
      if (data && data.ok) {
        var v = data.values || {};
        $('#s-receive').value = v.resend_receive_email || '';
        $('#s-from-name').value = v.resend_from_name || '';
        $('#s-from-email').value = v.resend_from_email || '';
        $('#s-key').value = '';
        $('#s-key-mask').textContent = v.resend_key_masked || '';
        $('#s-key-status').classList.toggle('hidden', !v.has_resend_key);
        $('#s-clear-key-wrap').classList.toggle('hidden', !v.has_resend_key);
        $('#s-clear-key').checked = false;
      }
      if (notice) { setFlash('ok', notice); }
    });
  }

  function testSettingsSend() {
    if (!requireApi()) { return; }
    if (!window.confirm('现在给接收邮箱发一封测试邮件？\n（会真的发送）')) { return; }
    setFlash('ok', '正在发送…');
    call('test_settings_send').then(function (res) {
      handleResult(res);
    });
  }

  /* ---------- 绑定 ---------- */

  /* 安全绑定：元素不存在时跳过而不是抛错。
     bind() 里任何一个 null 都会中断后面所有绑定 —— 曾因此导致整个界面空白。 */
  function on(selector, event, handler) {
    var node = typeof selector === 'string' ? $(selector) : selector;
    if (!node) {
      console.warn('元素不存在，跳过绑定：', selector);
      return null;
    }
    node.addEventListener(event, handler);
    return node;
  }

  function bind() {
    $$('.nav__link').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var view = btn.getAttribute('data-view');
        if (view === 'form') { openForm(null); return; }
        if (view === 'settings') { openSettings(); return; }
        show('timeline');
        loadTimeline();
      });
    });

    on('#btn-add', 'click', function () { openForm(null); });
    initBulkBar();
    on('#recipient-form', 'submit', submitForm);
    on('#settings-form', 'submit', submitSettings);
    on('#btn-test-settings', 'click', testSettingsSend);
    on('#f-solar', 'input', onSolarInput);

    on('#s-key-change', 'click', function () {
      $('#s-key-status').classList.add('hidden');
      $('#s-key-change').classList.add('hidden');
      $('#s-key').value = '';
      $('#s-key').focus();
    });

    // "取消"/"返回时间轴"按钮（表单与预览页都有）
    $$('[data-goto-timeline]').forEach(function (btn) {
      btn.addEventListener('click', function () { show('timeline'); loadTimeline(); });
    });

    // 顶部「设置」入口用导航按钮，不再依赖单独的 #btn-settings
  }

  /* ---------- 启动 ---------- */

  var booted = false;

  /* 性能打点：记录 DOM 就绪与首帧渲染时刻。
     诊断"打开慢"时有据可查，正常使用无感知。 */
  try {
    window.__DOM_READY_MS = Math.round(performance.now());
  } catch (e) { /* 忽略 */ }

  /* 首屏：用 Python 嵌进来的数据立即渲染。
     这一步不依赖 js_api，因此在 pywebview 注入完成前就能看到内容。 */
  function renderBootstrap() {
    var data = window.__BOOTSTRAP__;
    if (!data) { return false; }
    if (!data.ok) {
      setFlash('err', data.error || '读取数据失败');
      $('#timeline-sub').textContent = '读取失败';
      return true;
    }
    renderTimeline(data);
    try {
      window.__FIRST_PAINT_MS = Math.round(performance.now());
    } catch (e) { /* 打点失败不影响功能 */ }
    return true;  }

  function boot() {
    if (booted) { return; }        // 重复触发要幂等
    if (!window.pywebview || !window.pywebview.api) { return; }
    booted = true;
    api = window.pywebview.api;
    try {
      bind();
    } catch (err) {
      console.error('bind 失败', err);
      setFlash('err', '界面初始化失败：' + (err && err.message ? err.message : err));
      return;
    }
    // 首屏已渲染过就不重复请求；否则拉一次
    if (renderedFromBootstrap) {
      window.__BOOTSTRAP__ = null;   // 用完即弃，避免切回时又渲染旧数据
    } else {
      loadTimeline();
    }
  }

  var renderedFromBootstrap = renderBootstrap();

  /* pywebview 注入 api 的时机不确定：
     脚本在 body 末尾同步执行，此时 api 往往还没准备好；
     而 pywebviewready 事件也可能在脚本注册监听之前就已触发。
     只用事件会漏掉（事件已过），只用轮询会拖慢。
     因此两者都用，boot() 自身幂等，谁先到谁生效。 */
  window.addEventListener('pywebviewready', boot);

  (function waitForApi() {
    var tries = 0;
    (function tick() {
      if (booted) { return; }
      if (window.pywebview && window.pywebview.api) { boot(); return; }
      tries += 1;
      if (tries > 400) {           // 约 20 秒仍没有，说明注入失败
        setFlash('err', '后端未就绪：pywebview 没有注入 api。请查看 ~/.birthdayrs/app.log');
        return;
      }
      setTimeout(tick, 50);
    })();
  })();
})();
