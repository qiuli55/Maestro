(function(){
"use strict";

  // ========== WindowManager 多窗口系统 ==========
  var winContainer=document.getElementById("win-container");
  var winTabsEl=document.getElementById("win-tabs");
  var winZ=500; // z-index 计数器
  var wins={}; // id -> {el, data, minimized}
  var activeWinId=null;
  var WIN_OFFSET=50; // 窗口堆叠偏移（向上）

  // 窗口模板（无输入框，统一用底部 Dock 输入框）
  function createWinHTML(id,title,kind){
    return '<div class="win" data-id="'+id+'" style="display:none">'+
      '<div class="win-head" data-win="drag">'+
        '<span class="win-title">'+escapeHtml(title)+'</span>'+
        '<span class="win-kind">['+(kind==="task"?"任务":"聊天")+']</span>'+
        '<div class="win-btns">'+
          '<button class="win-btn link" data-win="link" title="关联其他对话的上下文">&#128279;</button>'+
          '<button class="win-btn min" data-win="min" title="最小化">&#x2212;</button>'+
          '<button class="win-btn max" data-win="max" title="最大化">&#x25A1;</button>'+
          '<button class="win-btn close" data-win="close" title="关闭">×</button>'+
        '</div>'+
      '</div>'+
      '<div class="win-links"></div>'+
      '<div class="win-body" id="win-body-'+id+'"><div class="win-msgs" data-conv="'+id+'"></div></div>'+
      '<div class="win-resize" data-win="resize"></div>'+
    '</div>';
  }

  // ===== 会话 <-> 窗口宿主关系 =====
  // convHost[convId]=winId：该会话当前住在哪个窗口（独立窗口 id 或合并窗口 id）
  // convMeta[convId]={title,kind}：会话元数据（拖出还原/合并时用）
  var convHost={};
  var convMeta={};
  var parking=document.getElementById("msg-parking");

  function msgsEl(convId){return document.querySelector('.win-msgs[data-conv="'+convId+'"]');}
  function winOfConv(cid){return convHost[cid]&&wins[convHost[cid]];}
  function ensureMsgsContainer(cid){
    var c=msgsEl(cid);
    if(c)return c;
    c=document.createElement("div");
    c.className="win-msgs";
    c.dataset.conv=cid;
    parking.appendChild(c);
    return c;
  }
  function parkMsgs(cid){
    var c=msgsEl(cid);
    if(c&&c.parentNode!==parking)parking.appendChild(c);
  }

  function bindWinEvents(w){
    var id=w.data.id;
    w.el.querySelector(".win-head").addEventListener("mousedown",function(e){winDragStart(e,id)});
    w.el.querySelector(".win-resize").addEventListener("mousedown",function(e){winResizeStart(e,id)});
    w.el.querySelectorAll(".win-btn").forEach(function(btn){
      btn.addEventListener("click",function(e){
        var act=btn.dataset.win;
        if(act==="min")winMinimize(id);
        else if(act==="max")winMaximize(id);
        else if(act==="close")winClose(id);
        else if(act==="splitall")dissolveMergedAll(id);
        else if(act==="link"){e.stopPropagation();toggleLinkPopover(id,btn);}
      });
    });
  }

  // 创建窗口
  function winCreate(id,title,kind){
    if(wins[id]){winActivate(id);return wins[id]}
    var el=document.createElement("div");
    el.innerHTML=createWinHTML(id,title,kind);
    el=el.firstElementChild;
    document.body.appendChild(el);
    // 位置：右下角向上堆叠（不遮挡 dock）
    var idx=Object.keys(wins).length;
    el.style.right="20px";
    el.style.bottom=(140+idx*WIN_OFFSET)+"px";
    wins[id]={el:el,data:{id:id,title:title,kind:kind},minimized:false};
    convHost[id]=id;
    convMeta[id]={title:title,kind:kind};
    bindWinEvents(wins[id]);
    winActivate(id);
    winTabsRender();
    saveWindowsState(); // 创建即保存，刷新后可恢复
    return wins[id];
  }

  // ===== 合并窗口：多标签大窗口 =====
  function mergedWinHTML(id){
    return '<div class="win merged" data-id="'+id+'" style="display:none">'+
      '<div class="win-head" data-win="drag">'+
        '<span class="win-title">合并窗口</span>'+
        '<span class="win-kind">[合并]</span>'+
        '<div class="win-btns">'+
          '<button class="win-btn link" data-win="link" title="关联当前标签对话的上下文">&#128279;</button>'+
          '<button class="win-btn split" data-win="splitall" title="全部拆分回独立窗口">&#x21C5;</button>'+
          '<button class="win-btn min" data-win="min" title="最小化">&#x2212;</button>'+
          '<button class="win-btn max" data-win="max" title="最大化">&#x25A1;</button>'+
          '<button class="win-btn close" data-win="close" title="拆分并关闭">&#xD7;</button>'+
        '</div>'+
      '</div>'+
      '<div class="win-links"></div>'+
      '<div class="mw-tabs"></div>'+
      '<div class="win-body" id="win-body-'+id+'"></div>'+
      '<div class="win-resize" data-win="resize"></div>'+
    '</div>';
  }
  function renderMergedTabs(m){
    var bar=m.el.querySelector(".mw-tabs");
    bar.innerHTML=m.data.children.map(function(cid){
      var meta=convMeta[cid]||{title:"对话",kind:"chat"};
      return '<span class="mw-tab'+(cid===m.activeChild?" active":"")+'" data-conv="'+cid+'" title="'+escapeHtml(meta.title)+'">'+
        escapeHtml(meta.title.slice(0,10))+'['+(meta.kind==="task"?"任务":"聊")+']</span>';
    }).join("");
  }
  function activateMergedTab(mergedId,cid){
    var m=wins[mergedId];
    if(!m||m.data.kind!=="merged"||m.data.children.indexOf(cid)<0)return;
    m.activeChild=cid;
    var body=m.el.querySelector(".win-body");
    body.querySelectorAll(".win-msgs").forEach(function(c){parkMsgs(c.dataset.conv);});
    body.appendChild(ensureMsgsContainer(cid));
    m.el.querySelector(".win-title").textContent=convMeta[cid]?convMeta[cid].title:"对话";
    renderMergedTabs(m);
    selectedWinId=cid; // 选中会话 = 激活的 tab
    updateDockPlaceholder();
    renderLinksBar(cid);
    saveWindowsState();
  }
  function bindMergedEvents(m){
    var bar=m.el.querySelector(".mw-tabs");
    bar.addEventListener("mousedown",function(e){
      var tab=e.target.closest(".mw-tab");
      if(!tab)return;
      e.stopPropagation(); // 不触发窗口拖动
      var cid=tab.dataset.conv;
      var sx=e.clientX,sy=e.clientY,dragged=false;
      function mm(ev){
        if(!dragged&&Math.abs(ev.clientX-sx)+Math.abs(ev.clientY-sy)>26){
          dragged=true;
          cleanup();
          detachConv(cid,ev.clientX,ev.clientY); // 拖出成独立窗口
        }
      }
      function mu(){cleanup();if(!dragged){activateMergedTab(m.data.id,cid);winActivate(m.data.id);}}
      function cleanup(){document.removeEventListener("mousemove",mm);document.removeEventListener("mouseup",mu);}
      document.addEventListener("mousemove",mm);
      document.addEventListener("mouseup",mu);
    });
  }
  function attachConvToMerged(cid,mergedId){
    var m=wins[mergedId];
    ensureMsgsContainer(cid);
    parkMsgs(cid);
    var host=convHost[cid];
    if(host&&wins[host]&&wins[host].data.kind!=="merged")removeStandaloneShell(host);
    convHost[cid]=mergedId;
    if(!convMeta[cid])convMeta[cid]={title:"对话",kind:"chat"};
    m.data.children.push(cid);
  }
  function removeStandaloneShell(winId){
    var w=wins[winId];
    if(!w)return;
    removeWinMini(winId);
    w.el.remove();
    delete wins[winId];
  }
  function mergeWindows(){
    var ids=Object.keys(wins).filter(function(k){return wins[k].data.kind!=="merged"&&!wins[k].minimized});
    if(ids.length<2)return;
    var mw=createMergedWindow(ids);
    winActivate(mw.data.id);
    activateMergedTab(mw.data.id,ids[0]);
  }
  function createMergedWindow(convIds){
    var id="merged_"+Date.now()+"_"+Math.random().toString(36).slice(2,6);
    var el=document.createElement("div");
    el.innerHTML=mergedWinHTML(id);
    el=el.firstElementChild;
    document.body.appendChild(el);
    // 位置：沿用第一个源窗口的位置；否则右下
    var src=null;
    for(var i=0;i<convIds.length;i++){if(wins[convIds[i]]){src=wins[convIds[i]];break;}}
    if(src&&src.el.style.left){el.style.left=src.el.style.left;el.style.top=src.el.style.top;el.style.right="";el.style.bottom="";}
    else{el.style.right="20px";el.style.bottom="150px";}
    el.style.width="640px";el.style.height="520px";
    var mw={el:el,data:{id:id,title:"合并窗口",kind:"merged",children:[]},minimized:false,activeChild:null};
    wins[id]=mw;
    bindWinEvents(mw);
    bindMergedEvents(mw);
    convIds.forEach(function(cid){attachConvToMerged(cid,id);});
    winTabsRender();
    saveWindowsState();
    return mw;
  }
  // Tab 拖出：把会话从合并窗口还原成独立窗口（落在鼠标位置）
  function detachConv(cid,x,y){
    var hostId=convHost[cid];
    var m=hostId&&wins[hostId];
    if(!m||m.data.kind!=="merged")return;
    var kids=m.data.children;
    var idx=kids.indexOf(cid);
    if(idx<0)return;
    kids.splice(idx,1);
    var meta=convMeta[cid]||{title:"对话",kind:"chat"};
    var rect=m.el.getBoundingClientRect();
    var w=createStandaloneShellFor(cid,meta,(x!=null?x:rect.left)-150,(y!=null?y:rect.top)-12);
    var shellBody=w.el.querySelector(".win-body");
    var c=ensureMsgsContainer(cid);
    shellBody.appendChild(c);
    convHost[cid]=w.data.id;
    renderLinksBar(cid);
    if(kids.length===0){
      // 合并窗口空了：直接移除壳
      removeWinMini(hostId);
      m.el.remove();
      delete wins[hostId];
    }else{
      if(m.activeChild===cid)m.activeChild=null;
      if(!m.activeChild)activateMergedTab(hostId,kids[0]);
      else renderMergedTabs(m);
    }
    winActivate(w.data.id);
    winTabsRender();
    saveWindowsState();
  }
  // 全部拆分（合并窗口 ✕ / ⇕）：子会话逐一还原为独立窗口，保留原位置
  function dissolveMergedAll(mergedId){
    var m=wins[mergedId];
    if(!m||m.data.kind!=="merged")return;
    var rect=m.el.getBoundingClientRect();
    var kids=m.data.children.slice();
    removeWinMini(mergedId);
    m.el.remove();
    delete wins[mergedId];
    kids.forEach(function(cid,i){
      var meta=convMeta[cid]||{title:"对话",kind:"chat"};
      var sw=createStandaloneShellFor(cid,meta,rect.left+i*30,rect.top+i*30);
      var shellBody=sw.el.querySelector(".win-body");
      shellBody.appendChild(ensureMsgsContainer(cid));
      convHost[cid]=sw.data.id;
      renderLinksBar(cid);
    });
    winTabsRender();
    saveWindowsState();
  }
  function createStandaloneShellFor(cid,meta,x,y){
    var el=document.createElement("div");
    el.innerHTML=createWinHTML(cid,meta.title,meta.kind);
    el=el.firstElementChild;
    document.body.appendChild(el);
    if(x!=null){el.style.left=Math.max(0,x)+"px";el.style.top=Math.max(0,y)+"px";el.style.right="";el.style.bottom="";}
    var w={el:el,data:{id:cid,title:meta.title,kind:meta.kind},minimized:false};
    wins[w.data.id]=w;
    convMeta[cid]=meta;
    bindWinEvents(w);
    return w;
  }

  // 激活窗口
  function winActivate(id){
    var w=wins[id];
    if(!w)return;
    if(activeWinId&&wins[activeWinId]){
      wins[activeWinId].el.style.zIndex=winZ++;
    }
    activeWinId=id;
    // 选中会话：合并窗口 -> 当前 tab 的会话；独立窗口 -> 自身
    if(w.data.kind==="merged"){
      var child=w.activeChild||w.data.children[0];
      if(child)activateMergedTab(id,child);
      else selectedWinId=null;
    }else{
      selectedWinId=id;
    }
    w.el.style.zIndex=winZ++;
    w.el.style.display="flex";
    w.el.classList.remove("minimized");
    w.minimized=false;
    // 移除小卡片
    removeWinMini(id);
    // 更新焦点视觉状态
    updateWinFocus(id);
    winTabsRender();
    winSelectRender();
    // 动态更新 dock 输入框 placeholder
    updateDockPlaceholder();
    dockInput.focus(); // 聚焦到底部输入框
  }

  // 更新所有窗口的焦点状态（给选中窗口加 .focused）
  function updateWinFocus(focusedId){
    Object.keys(wins).forEach(function(k){
      if(k===focusedId) wins[k].el.classList.add("focused");
      else wins[k].el.classList.remove("focused");
    });
  }

  // 更新 dock 输入框 placeholder 为当前选中窗口名
  function updateDockPlaceholder(){
    var meta=selectedWinId&&convMeta[selectedWinId];
    if(meta){
      dockInput.placeholder="向【"+meta.title.slice(0,8)+"】发送消息…";
    }else{
      dockInput.placeholder="选择一个窗口或新建窗口后发送…";
    }
  }

  // 最小化窗口
  function winMinimize(id){
    var w=wins[id];
    if(!w)return;
    w.minimized=true;
    w.el.classList.add("minimized");
    // 创建小卡片
    createWinMini(id);
    if(activeWinId===id){
      // 激活其他窗口
      var others=Object.keys(wins).filter(function(k){return k!==id&&!wins[k].minimized});
      if(others.length)winActivate(others[others.length-1]);
      activeWinId=null;
    }
    winTabsRender();
  }

  // 创建窗口小卡片
  function createWinMini(id){
    var w=wins[id];
    if(!w)return;
    // 如果已存在小卡片，先移除
    var existing=document.getElementById("win-mini-"+id);
    if(existing){existing.remove();}
    var mini=document.createElement("div");
    mini.id="win-mini-"+id;
    mini.className="win-mini";
    mini.innerHTML='<div class="win-mini-title">'+escapeHtml(w.data.title)+'</div>'+
      '<div class="win-mini-kind">['+(w.data.kind==="task"?"任务":"聊天")+']</div>'+
      '<div class="win-mini-drag" data-mini="drag"></div>';
    document.body.appendChild(mini);
    // 点击小卡片恢复窗口
    mini.addEventListener("click",function(e){
      if(e.target.dataset.mini==="drag")return;
      winActivate(id);
    });
    // 小卡片拖动
    var miniDrag=null,miniOffX=0,miniOffY=0;
    mini.querySelector(".win-mini-drag").addEventListener("mousedown",function(e){
      miniDrag=mini;
      var rect=mini.getBoundingClientRect();
      miniOffX=e.clientX-rect.left;
      miniOffY=e.clientY-rect.top;
      e.stopPropagation();
    });
    document.addEventListener("mousemove",function(e){
      if(!miniDrag)return;
      miniDrag.style.left=(e.clientX-miniOffX)+"px";
      miniDrag.style.top=(e.clientY-miniOffY)+"px";
      miniDrag.style.right="";miniDrag.style.bottom="";
    });
    document.addEventListener("mouseup",function(){
      miniDrag=null;
    });
  }

  // 移除窗口小卡片
  function removeWinMini(id){
    var mini=document.getElementById("win-mini-"+id);
    if(mini){mini.remove();}
  }

  // 最大化窗口（简化：全屏）
  function winMaximize(id){
    var w=wins[id];
    if(!w)return;
    if(w.el.style.width==="100vw"){
      // 恢复
      w.el.style.width="";w.el.style.height="";w.el.style.right="";w.el.style.bottom="";
    }else{
      w.el.style.width="100vw";w.el.style.height="100vh";
      w.el.style.right="0";w.el.style.bottom="0";
    }
  }

  // 关闭窗口（合并窗口：全部拆分回独立窗口后再关闭合并壳，不丢会话）
  function winClose(id){
    var w=wins[id];
    if(!w)return;
    if(w.data.kind==="merged"){
      dissolveMergedAll(id);
      if(activeWinId===id){
        activeWinId=null;
        var others=Object.keys(wins).filter(function(k){return !wins[k].minimized});
        if(others.length)winActivate(others[others.length-1]);
        else{selectedWinId=null;updateDockPlaceholder()}
      }
      return;
    }
    removeWinMini(id); // 移除小卡片
    w.el.remove();
    delete wins[id];
    delete convHost[id];
    if(activeWinId===id){
      var others=Object.keys(wins).filter(function(k){return !wins[k].minimized});
      if(others.length)winActivate(others[others.length-1]);
      else{activeWinId=null;selectedWinId=null;updateDockPlaceholder()}
    }
    winTabsRender();
    saveWindowsState(); // 保存窗口状态到 localStorage
  }

  // 保存窗口状态到 localStorage（独立窗口 + 合并窗口）
  function saveWindowsState(){
    var state=Object.keys(wins).map(function(k){
      var w=wins[k];
      var el=w.el;
      var pos={left:el.style.left,top:el.style.top,right:el.style.right,
               bottom:el.style.bottom,width:el.style.width,height:el.style.height};
      if(w.data.kind==="merged"){
        return {merged:w.data.children.map(function(cid){
                   return {id:cid,title:convMeta[cid].title,kind:convMeta[cid].kind};
                 }),
                activeChild:w.activeChild,minimized:w.minimized,pos:pos};
      }
      return {id:w.data.id,title:w.data.title,kind:w.data.kind,minimized:w.minimized,pos:pos};
    });
    localStorage.setItem("maestro_windows",JSON.stringify(state));
  }

  // 从 localStorage 恢复窗口状态（独立 + 合并）
  function restoreWindows(){
    try{
      var data=localStorage.getItem("maestro_windows");
      if(!data)return;
      var state=JSON.parse(data);
      if(!Array.isArray(state)||state.length===0)return;
      state.forEach(function(s){
        var pos=s.pos||s; // 兼容旧格式
        if(s.merged&&s.merged.length){
          s.merged.forEach(function(c){convMeta[c.id]={title:c.title,kind:c.kind};});
          var mw=createMergedWindow(s.merged.map(function(c){return c.id;}));
          var el=mw.el;
          if(pos.left)el.style.left=pos.left;
          if(pos.top)el.style.top=pos.top;
          if(pos.right)el.style.right=pos.right;
          if(pos.bottom)el.style.bottom=pos.bottom;
          if(pos.width)el.style.width=pos.width;
          if(pos.height)el.style.height=pos.height;
          if(s.activeChild)activateMergedTab(mw.data.id,s.activeChild);
          if(s.minimized)winMinimize(mw.data.id);
          return;
        }
        var w=winCreate(s.id,s.title,s.kind);
        if(!w)return;
        var el=w.el;
        if(pos.left)el.style.left=pos.left;
        if(pos.top)el.style.top=pos.top;
        if(pos.right)el.style.right=pos.right;
        if(pos.bottom)el.style.bottom=pos.bottom;
        if(pos.width)el.style.width=pos.width;
        if(pos.height)el.style.height=pos.height;
        if(s.minimized){
          winMinimize(s.id);
        }
      });
      backfillAllHistories(); // 历史回灌：恢复的窗口拉取各自会话的历史消息
    }catch(e){
      console.error("恢复窗口失败:",e);
    }
  }

  // 历史回灌：为所有已恢复的会话窗口拉取最近消息（刷新后窗口不再空白）。
  // 消息容器可能住在独立窗口或合并窗口 tab，msgsEl 按 convId 寻址天然兼容。
  function backfillAllHistories(){
    Object.keys(convMeta).forEach(function(cid){
      var body=msgsEl(cid);
      if(!body||body.childElementCount>0)return; // 已有内容（异常场景）不重复灌
      fetch(MAESTRO+"/api/chat/history?limit=30&conv_id="+encodeURIComponent(cid),{cache:"no-store"})
        .then(function(r){return r.ok?r.json():null})
        .then(function(d){
          var msgs=d&&d.messages;
          if(!msgs||!msgs.length)return;
          var b=msgsEl(cid);
          if(!b||b.childElementCount>0)return; // 拉取期间用户已发消息：放弃回灌
          msgs.forEach(function(m){
            if(m.role!=="user"&&m.role!=="assistant")return;
            winAppendMsg(cid,m.role,m.content);
          });
          b.dataset.backfilled="1";
        })
        .catch(function(){ /* 服务未起：静默，用户仍可发新消息 */ });
    });
  }

  // 页面加载完成后恢复窗口
  setTimeout(restoreWindows,500);

  // ========== 消息渲染引擎：Markdown / 头像 / 时间分隔 / 操作按钮 ==========
  var MD_FOLD_LEN = 1400;          // 超过该长度折叠"展开全文"
  var AVATAR_HTML = '<img class="msg-avatar" src="pet_frieren_q.png" alt="">';

  // 行内 Markdown（先转义再替换，天然防注入）
  function mdInline(raw) {
    var s = escapeHtml(raw);
    s = s.replace(/`([^`]+)`/g, function(_, c) { return '<code class="md-inline">' + c + '</code>'; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/~~([^~]+)~~/g, "<s>$1</s>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s.replace(/\n/g, "<br>");
  }

  function mdCodeBlock(c) {
    return '<div class="md-code"><div class="md-code-bar"><span>' + escapeHtml(c.lang || "代码") + '</span>' +
      '<button type="button" class="md-code-copy">复制代码</button></div>' +
      '<pre><code>' + escapeHtml(c.code) + '</code></pre></div>';
  }

  // 块级 Markdown：围栏代码 / 标题 / 列表 / 引用 / 分隔线 / 段落
  function mdRender(src) {
    src = String(src || "").replace(/\r\n/g, "\n").trim();
    if (!src) return "";
    var codes = [];
    src = src.replace(/```([\w+#.-]*)[ \t]*\n?([\s\S]*?)```/g, function(_, lang, code) {
      codes.push({ lang: lang, code: code.replace(/\n$/, "") });
      return "\x00C" + (codes.length - 1) + "\x00";
    });
    var lines = src.split("\n"), out = [], list = null, quote = false, para = [];
    function flushPara() { if (para.length) { out.push("<p>" + mdInline(para.join("\n")) + "</p>"); para = []; } }
    function closeList() { if (list) { out.push(list === "ul" ? "</ul>" : "</ol>"); list = null; } }
    function closeQuote() { if (quote) { out.push("</blockquote>"); quote = false; } }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var cm = ln.match(/^\x00C(\d+)\x00\s*$/);
      if (cm) { flushPara(); closeList(); closeQuote(); out.push(mdCodeBlock(codes[+cm[1]])); continue; }
      if (!ln.trim()) { flushPara(); closeList(); closeQuote(); continue; }
      var h = ln.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushPara(); closeList(); closeQuote(); out.push("<h" + h[1].length + ">" + mdInline(h[2]) + "</h" + h[1].length + ">"); continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(ln.trim())) { flushPara(); closeList(); closeQuote(); out.push("<hr>"); continue; }
      var q = ln.match(/^>\s?(.*)$/);
      if (q) { flushPara(); closeList(); if (!quote) { out.push("<blockquote>"); quote = true; } out.push("<p>" + mdInline(q[1]) + "</p>"); continue; }
      closeQuote();
      var ul = ln.match(/^\s*[-*+]\s+(.*)$/);
      var ol = ln.match(/^\s*\d+[.、)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        var tag = ul ? "ul" : "ol";
        if (list !== tag) { closeList(); out.push("<" + tag + ">"); list = tag; }
        out.push("<li>" + mdInline((ul || ol)[1]) + "</li>");
        continue;
      }
      closeList();
      para.push(ln);
    }
    flushPara(); closeList(); closeQuote();
    return out.join("").replace(/\x00C(\d+)\x00/g, function(_, n) { return mdCodeBlock(codes[+n]); });
  }

  // 长回复折叠：>MD_FOLD_LEN 时收起并给"展开全文"按钮
  function applyFold(div, text) {
    if (text.length <= MD_FOLD_LEN || div.classList.contains("folded") || div.querySelector(".md-fold-btn")) return;
    div.classList.add("folded");
    var meta = div.querySelector(".msg-meta");
    if (meta) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "md-fold-btn";
      b.textContent = "展开全文";
      meta.appendChild(b);
    }
  }

  // 消息 DOM 构造（头像/操作条/时间分隔/连续合并）
  function buildMsgEl(role, text, cont) {
    var div = document.createElement("div");
    div.className = "win-msg " + role + (cont ? " cont" : "") + (role === "assistant" ? " md-msg" : "");
    div.dataset.text = text;
    var contentHtml = role === "assistant" ? mdRender(text) : escapeHtml(text);
    div.innerHTML = (role === "assistant" ? AVATAR_HTML : "") +
      '<div class="msg-content">' + contentHtml + '</div>' +
      '<div class="msg-meta"><span class="win-time">' + new Date().toLocaleTimeString() + '</span></div>';
    var ops = document.createElement("div");
    ops.className = "msg-ops";
    ops.innerHTML = '<button type="button" data-op="copy" title="复制">复制</button>' +
      (role === "user" ? '<button type="button" data-op="resend" title="重新发送">重发</button>'
                       : '<button type="button" data-op="quote" title="引用到输入框">引用</button>');
    div.appendChild(ops);
    return div;
  }

  // 打字指示（芙莉莲正在输入…）——按会话寻址，容器无论在独立窗口还是合并窗口都能找到
  function showTyping(id) {
    var body = msgsEl(id);
    if (!body) return;
    hideTyping(id);
    var div = document.createElement("div");
    div.className = "win-msg assistant typing" + (body._lastMsg && body._lastMsg.role === "assistant" ? " cont" : "");
    div.innerHTML = AVATAR_HTML + '<div class="msg-content"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>';
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }
  function hideTyping(id) {
    var t = msgsEl(id) && msgsEl(id).querySelector(".win-msg.typing");
    if (t) t.remove();
  }

  // ========== 跨对话上下文关联 UI ==========
  // convLinks[convId]=[其他 convId,...]（数据层定义在会话隔离区），随聊天请求发给后端
  var linkPop = null;

  // 窗口里的关联栏：显示该会话当前关联了谁，可单个移除
  function renderLinksBar(convId) {
    var host = winOfConv(convId);
    if (!host) return;
    var bar = host.el.querySelector(".win-links");
    if (!bar) return;
    var links = (convLinks[convId] || []).filter(function(cid) { return convMeta[cid]; });
    var btn = host.el.querySelector('.win-btn[data-win="link"]');
    if (!links.length) {
      bar.classList.remove("show");
      bar.innerHTML = "";
      if (btn) { btn.classList.remove("has-links"); btn.removeAttribute("data-n"); }
      return;
    }
    bar.classList.add("show");
    bar.innerHTML = "<span>关联上下文：</span>" + links.map(function(cid) {
      var t = convMeta[cid] ? convMeta[cid].title : cid;
      return '<span class="lk"><b>' + escapeHtml(String(t).slice(0, 10)) + '</b><i data-unlink="' + cid + '" title="移除关联">×</i></span>';
    }).join("");
    if (btn) { btn.classList.add("has-links"); btn.setAttribute("data-n", String(links.length)); }
  }

  // 🔗 按钮：弹出其他会话勾选列表（勾选即保存；合并窗口作用于当前激活 tab 的会话）
  function toggleLinkPopover(winId, anchorBtn) {
    if (!linkPop) {
      linkPop = document.createElement("div");
      linkPop.id = "link-pop";
      document.body.appendChild(linkPop);
      document.addEventListener("mousedown", function(e) {
        if (linkPop && linkPop.classList.contains("show") &&
            !e.target.closest("#link-pop") && !e.target.closest('.win-btn[data-win="link"]')) {
          linkPop.classList.remove("show");
        }
      });
    }
    var w = wins[winId];
    if (!w) return;
    var convId = w.data.kind === "merged" ? w.activeChild : winId;
    if (!convId) return;
    if (linkPop.classList.contains("show") && linkPop.dataset.conv === convId) {
      linkPop.classList.remove("show");
      return;
    }
    linkPop.dataset.conv = convId;
    var others = Object.keys(convMeta).filter(function(cid) { return cid !== convId; });
    var links = convLinks[convId] || [];
    linkPop.innerHTML = '<div class="lp-title">关联其他对话的上下文</div>' +
      (others.length ? others.map(function(cid) {
        var m = convMeta[cid];
        var checked = links.indexOf(cid) >= 0 ? " checked" : "";
        return '<label class="lp-item"><input type="checkbox" data-cid="' + cid + '"' + checked +
          '><span>' + escapeHtml(String(m.title || cid).slice(0, 14)) + '[' + (m.kind === "task" ? "任务" : "聊") + ']</span></label>';
      }).join("") : '<div class="lp-empty">没有其他打开的窗口</div>') +
      '<div class="lp-hint">勾选后，该对话发消息时会把对方最近几轮内容作为参考上下文发给模型。</div>';
    linkPop.classList.add("show");
    if (anchorBtn) {
      var r = anchorBtn.getBoundingClientRect();
      linkPop.style.left = Math.max(8, Math.min(r.left - 90, innerWidth - 252)) + "px";
      linkPop.style.top = Math.max(8, r.top - linkPop.offsetHeight - 8) + "px";
    }
    linkPop.querySelectorAll("input[type=checkbox]").forEach(function(cb) {
      cb.addEventListener("change", function() {
        var arr = convLinks[convId] || (convLinks[convId] = []);
        var cid = cb.dataset.cid;
        if (cb.checked) { if (arr.indexOf(cid) < 0) arr.push(cid); }
        else { convLinks[convId] = arr.filter(function(x) { return x !== cid; }); }
        if (!convLinks[convId].length) delete convLinks[convId];
        saveConvLinks();
        renderLinksBar(convId);
      });
    });
  }

  // 操作按钮 / 折叠 / 代码复制：事件委托（气泡内容会被流式重写，不能逐个绑）
  document.addEventListener("click", function(e) {
    var opBtn = e.target.closest(".msg-ops button");
    if (opBtn) {
      var msg = opBtn.closest(".win-msg");
      var winEl = opBtn.closest(".win");
      if (!msg || !winEl) return;
      var op = opBtn.dataset.op;
      if (op === "copy") {
        if (navigator.clipboard) navigator.clipboard.writeText(msg.dataset.text || "").catch(function() {});
      } else if (op === "copytask") {
        var taskEl = opBtn.closest(".task-card");
        var resEl2 = taskEl && taskEl.querySelector(".tc-result");
        var txt2 = (resEl2 && resEl2.dataset.raw) || (resEl2 && resEl2.textContent) || "";
        if (navigator.clipboard) navigator.clipboard.writeText(txt2).catch(function() {});
        opBtn.textContent = "已复制";
        setTimeout(function() { opBtn.textContent = "复制结果"; }, 1200);
      } else if (op === "resend") {
        var winId = winEl.dataset.id;
        if (winId && wins[winId]) doSend(winId, msg.dataset.text || "");
      } else if (op === "quote") {
        dockInput.value = "> " + (msg.dataset.text || "").slice(0, 200) + "\n";
        dockInput.focus();
      }
      return;
    }
    var unlink = e.target.closest("[data-unlink]");
    if (unlink) {
      var winEl2 = unlink.closest(".win");
      if (winEl2) {
        var cid2 = winEl2.dataset.id;
        if (wins[cid2] && wins[cid2].data.kind === "merged") cid2 = wins[cid2].activeChild;
        if (cid2 && convLinks[cid2]) {
          convLinks[cid2] = convLinks[cid2].filter(function(x) { return x !== unlink.dataset.unlink; });
          if (!convLinks[cid2].length) delete convLinks[cid2];
          saveConvLinks();
          renderLinksBar(cid2);
        }
      }
      return;
    }
    var foldBtn = e.target.closest(".md-fold-btn");
    if (foldBtn) {
      var mb = foldBtn.closest(".win-msg");
      mb.classList.toggle("folded");
      foldBtn.textContent = mb.classList.contains("folded") ? "展开全文" : "收起";
      return;
    }
    var copyBtn = e.target.closest(".md-code-copy");
    if (copyBtn) {
      var code = copyBtn.closest(".md-code").querySelector("code");
      if (code && navigator.clipboard) navigator.clipboard.writeText(code.textContent).catch(function() {});
      copyBtn.textContent = "已复制";
      setTimeout(function() { copyBtn.textContent = "复制代码"; }, 1200);
    }
  });

  // 窗口内追加消息（Markdown 渲染 / 时间分隔 / 连续合并 / 清除打字指示）
  // 按 convId 寻址：消息容器可能住在独立窗口，也可能住在合并窗口的激活 tab 里
  function winAppendMsg(id,role,text){
    var w=winOfConv(id);
    if(!w)return;
    if(role==="assistant")hideTyping(id);
    var body=msgsEl(id);
    if(!body)return;
    var now=maybeAppendDivider(body);
    var last=body._lastMsg;
    var cont=last&&last.role===role&&(now-last.t)<120000;
    var div=buildMsgEl(role,text,cont);
    body.appendChild(div);
    if(role==="assistant"&&text.length>MD_FOLD_LEN)applyFold(div,text);
    body.scrollTop=body.scrollHeight;
    body._lastMsg={t:now,role:role};
    // 任务会话：提交后标题加 ⚙
    if(convMeta[id]&&convMeta[id].kind==="task"&&text.includes("任务已提交")){
      var titleEl=w.el.querySelector(".win-title");
      if(titleEl&&!titleEl.textContent.includes("⚙")){
        titleEl.textContent="⚙ "+titleEl.textContent;
      }
    }
  }

  // ========== WebSocket 事件通道 ==========
  // 后端 /ws 推送 {kind: chat|task|approval, conv_id, role, content}。
  // - task/approval 事件驱动刷新任务卡片（轮询降级为兜底）
  // - chat 消息仍走 winAppendMsg（带去重），WS 到达的聊天全文用于
  //   本地流式失败时的兜底（dataset.text 比对在 winAppendMsg 内完成）
  var evtWs=null,evtWsTimer=null;
  function connectEvtWs(){
    try{ evtWs=new WebSocket(MAESTRO.replace("http","ws")+"/ws"); }catch(e){ scheduleEvtWsReconnect(); return; }
    evtWs.onopen=function(){ /* 已连接：静默 */ };
    evtWs.onmessage=function(ev){
      var msg; try{ msg=JSON.parse(ev.data); }catch(e){ return; }
      var kind=msg.kind||"chat";
      var convId=msg.conv_id||"";
      if(kind==="approval"){
        showApprovalCard(msg);
        return;
      }
      if(kind==="task"&&convId){
        // 任务状态变化：拉一次快照刷新对应卡片（比解析 content 稳）
        fetch(MAESTRO+"/api/tasks/"+encodeURIComponent(msg.taskId||""),{cache:"no-store"})
          .then(function(r){return r.ok?r.json():null})
          .then(function(snap){ if(snap&&snap.task)renderTaskCard(convId,snap); })
          .catch(function(){});
        return;
      }
      if(kind==="chat"&&convId&&wins[convId]){
        winAppendMsg(convId,msg.role||"assistant",msg.content||"");
      }
    };
    evtWs.onclose=function(){ scheduleEvtWsReconnect(); };
    evtWs.onerror=function(){ try{evtWs.close();}catch(e){} };
  }
  function scheduleEvtWsReconnect(){
    if(evtWsTimer)return;
    evtWsTimer=setTimeout(function(){evtWsTimer=null;connectEvtWs();},3000);
  }
  setTimeout(connectEvtWs,800); // 页面加载后接入
  // e2e/测试调试口：喂一条伪造的 WS 消息走真实分发逻辑（不暴露写状态）
  window.__evtWsFeed=function(msgObj){
    if(evtWs&&evtWs.onmessage)evtWs.onmessage({data:JSON.stringify(msgObj)});
  };

  // ========== 审批卡片（沙箱越权命令批准/拒绝） ==========
  var TC_APPROVALS = {}; // approval_id -> {convId}
  function showApprovalCard(msg){
    var payload;
    try{ payload=typeof msg.content==="string"?JSON.parse(msg.content):msg.content; }
    catch(e){ return; }
    var approvalId=payload.approval_id||"";
    var taskId=payload.task_id||"";
    var cmd=payload.cmd||"";
    if(!approvalId||TC_APPROVALS[approvalId])return;
    // 找到该任务的会话窗口；找不到就挂到 dock 选中的窗口
    var convId=(taskId&&taskConvOf(taskId))||(selectedWinId&&convMeta[selectedWinId]?selectedWinId:null);
    if(!convId||!winOfConv(convId))return;
    TC_APPROVALS[approvalId]={convId:convId};
    var body=msgsEl(convId);
    if(!body)return;
    var now=maybeAppendDivider(body);
    var card=document.createElement("div");
    card.className="task-card approval-card";
    card.dataset.approval=approvalId;
    card.innerHTML=
      '<div class="tc-head"><span class="tc-status running">待审批</span>'+
        '<span class="tc-prompt">沙箱请求执行命令</span></div>'+
      '<div class="tc-meta"><code class="md-inline">'+escapeHtml(cmd.slice(0,120))+'</code></div>'+
      '<div class="tc-approve-row">'+
        '<button type="button" class="ap-approve">批准执行</button>'+
        '<button type="button" class="ap-reject">拒绝</button>'+
      '</div>';
    var ops=document.createElement("div");
    ops.className="msg-ops";
    ops.innerHTML='<button type="button" data-op="copytask" title="复制命令">复制命令</button>';
    card.appendChild(ops);
    // 复制命令复用 data.raw
    card.querySelector('[data-op="copytask"]').addEventListener("click",function(){
      if(navigator.clipboard)navigator.clipboard.writeText(cmd).catch(function(){});
    });
    function decide(action){
      fetch(MAESTRO+"/api/tasks/"+encodeURIComponent(taskId)+"/approvals/"+encodeURIComponent(approvalId),
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:action})})
        .then(function(r){return r.json()})
        .then(function(d){
          var chip=card.querySelector(".tc-status");
          if(d&&d.ok){
            chip.textContent=action==="approve"?"已批准":"已拒绝";
            chip.className="tc-status "+(action==="approve"?"done":"failed");
          }else{
            chip.textContent="已过期";
            chip.className="tc-status pending";
          }
          card.querySelector(".tc-approve-row").style.display="none";
        })
        .catch(function(){
          card.querySelector(".tc-status").textContent="网络错误";
        });
    }
    card.querySelector(".ap-approve").addEventListener("click",function(){decide("approve");});
    card.querySelector(".ap-reject").addEventListener("click",function(){decide("reject");});
    body.appendChild(card);
    body.scrollTop=body.scrollHeight;
    body._lastMsg={t:now,role:"assistant"};
  }
  // taskId -> convId 反查（遍历 convHost 的宿主窗口子卡片）
  function taskConvOf(taskId){
    var card=document.querySelector('.task-card[data-task="'+taskId+'"]');
    if(!card)return null;
    var cont=card.closest(".win-msgs");
    return cont?cont.dataset.conv:null;
  }

  // ========== 任务进度卡片 ==========
  var TC_STATUS={pending:"待拆分",ready:"待确认",running:"运行中",done:"已完成",
                 failed:"已失败",retry:"重试中",cancelled:"已取消"};

  // 首条消息或距上一条 >10 分钟时插时间分隔线，返回当前时间戳
  function maybeAppendDivider(body){
    var now=Date.now(),last=body._lastMsg;
    if(!last||now-last.t>600000){
      var dv=document.createElement("div");
      dv.className="msg-divider";
      var d=new Date();
      var sameDay=last&&new Date(last.t).toDateString()===d.toDateString();
      dv.textContent=(sameDay?"":(d.getMonth()+1)+"-"+d.getDate()+" ")+
        ("00"+d.getHours()).slice(-2)+":"+("00"+d.getMinutes()).slice(-2);
      body.appendChild(dv);
    }
    return now;
  }

  // 任务提交 → 富卡片（状态/进度条/子任务实时状态/结果区），自带 2s 轮询到终态
  function winAppendTaskCard(id,taskId,agents,model,prompt){
    var w=winOfConv(id);
    if(!w)return;
    hideTyping(id);
    var body=msgsEl(id);
    if(!body)return;
    var now=maybeAppendDivider(body);
    var card=document.createElement("div");
    card.className="task-card";
    card.dataset.task=taskId;
    card.innerHTML=
      '<div class="tc-head"><span class="tc-status pending">待拆分</span>'+
        '<span class="tc-prompt">'+escapeHtml((prompt||"").slice(0,50))+'</span></div>'+
      '<div class="tc-meta">'+escapeHtml(agents.join(" + "))+(model?" · "+escapeHtml(model):"")+" · "+escapeHtml(taskId)+'</div>'+
      '<div class="tc-bar"><i></i></div>'+
      '<button type="button" class="tc-toggle" style="display:none">子任务</button>'+
      '<div class="tc-subs"></div>'+
      '<div class="tc-result"></div>';
    var ops=document.createElement("div");
    ops.className="msg-ops";
    ops.innerHTML='<button type="button" data-op="copytask" title="复制结果">复制结果</button>';
    card.appendChild(ops);
    body.appendChild(card);
    body.scrollTop=body.scrollHeight;
    body._lastMsg={t:now,role:"assistant"};
    var titleEl=w.el.querySelector(".win-title");
    if(titleEl&&!titleEl.textContent.includes("⚙")){
      titleEl.textContent="⚙ "+titleEl.textContent;
    }
    pollTaskCard(id,taskId);
  }

  // 快照 → 卡片 DOM（WS 推送与轮询共用）
  var TC_FINAL = {done:1, failed:1, cancelled:1};
  function renderTaskCard(convId, snap){
    var taskId = snap.task.id;
    if(!winOfConv(convId))return false; // 会话窗口已关闭
    var card=document.querySelector('.task-card[data-task="'+taskId+'"]');
    if(!card)return false;
    var t=snap.task;
        var chip=card.querySelector(".tc-status");
        chip.textContent=TC_STATUS[t.status]||t.status;
        chip.className="tc-status "+t.status;
        card.classList.toggle("done-card",t.status==="done");
        card.classList.toggle("failed-card",t.status==="failed");
        var subs=snap.subtasks||[];
        if(subs.length){
          var finN=subs.filter(function(s){return s.status==="done"||s.status==="failed"||s.status==="cancelled"}).length;
          card.querySelector(".tc-bar i").style.width=Math.round(finN/subs.length*100)+"%";
          var doneN=subs.filter(function(s){return s.status==="done"}).length;
          var subsEl=card.querySelector(".tc-subs");
          subsEl.innerHTML=subs.map(function(s){
            var err=s.error?' <span class="tc-err">'+escapeHtml(String(s.error).slice(0,60))+"</span>":"";
            return '<div class="tc-sub"><span class="st '+s.status+'">'+(TC_STATUS[s.status]||s.status)+
              '</span><span>'+escapeHtml((s.desc||"").slice(0,42))+err+"</span></div>";
          }).join("");
          var tg=card.querySelector(".tc-toggle");
          tg.style.display="";
          tg.textContent="子任务 "+doneN+"/"+subs.length;
        }
    if(TC_FINAL[t.status]){
      // 终态：展示结果（Markdown），过长的结果折叠
      var resEl=card.querySelector(".tc-result");
      var txt=(t.result||"").trim();
      if(!txt&&t.status==="failed")txt="任务失败："+(t.error||"未知错误");
      if(txt){
        resEl.classList.add("show");
        resEl.dataset.raw=txt;
        resEl.innerHTML=mdRender(txt);
        if(txt.length>MD_FOLD_LEN && !card.querySelector(".tc-result-toggle")){
          resEl.classList.add("clamped");
          var rb=document.createElement("button");
          rb.type="button";rb.className="tc-result-toggle";rb.textContent="展开全文";
          rb.addEventListener("click",function(){
            resEl.classList.toggle("clamped");
            rb.textContent=resEl.classList.contains("clamped")?"展开全文":"收起";
          });
          card.appendChild(rb);
        }
      }
      poll(); // 刷新顶部编排器面板
      return false; // 终态：调用方停止轮询
    }
    return true; // 仍在运行：继续轮询
  }

  function pollTaskCard(convId,taskId){
    fetch(MAESTRO+"/api/tasks/"+taskId,{cache:"no-store"})
      .then(function(r){return r.json()})
      .then(function(snap){
        if(!snap||!snap.task)return;
        if(!renderTaskCard(convId,snap))return; // 终态或窗口关闭：停
        setTimeout(function(){pollTaskCard(convId,taskId)},2000);
      })
      .catch(function(){setTimeout(function(){pollTaskCard(convId,taskId)},4000)});
  }

  // 窗口拖动
  var dragWin=null,dragOffX=0,dragOffY=0;
  function winDragStart(e,id){
    if(e.target.dataset.win)return;
    var w=wins[id];
    if(!w)return;
    dragWin=w;
    var rect=w.el.getBoundingClientRect();
    dragOffX=e.clientX-rect.left;
    dragOffY=e.clientY-rect.top;
    winActivate(id);
    document.addEventListener("mousemove",winDragMove);
    document.addEventListener("mouseup",winDragEnd);
  }
  function winDragMove(e){
    if(!dragWin)return;
    // 边界检测：限制在屏幕内
    var minX=0,minY=0,maxX=window.innerWidth-200,maxY=window.innerHeight-100;
    var x=Math.max(minX,Math.min(e.clientX-dragOffX,maxX));
    var y=Math.max(minY,Math.min(e.clientY-dragOffY,maxY));
    dragWin.el.style.left=x+"px";
    dragWin.el.style.top=y+"px";
    dragWin.el.style.right="";dragWin.el.style.bottom="";
  }
  function winDragEnd(){
    dragWin=null;
    document.removeEventListener("mousemove",winDragMove);
    document.removeEventListener("mouseup",winDragEnd);
  }

  // 窗口调整大小
  var resizeWin=null,resizeW=0,resizeH=0,resizeX=0,resizeY=0;
  function winResizeStart(e,id){
    var w=wins[id];
    if(!w)return;
    resizeWin=w;
    var rect=w.el.getBoundingClientRect();
    resizeW=rect.width;resizeH=rect.height;
    resizeX=e.clientX;resizeY=e.clientY;
    winActivate(id);
    document.addEventListener("mousemove",winResizeMove);
    document.addEventListener("mouseup",winResizeEnd);
  }
  function winResizeMove(e){
    if(!resizeWin)return;
    var dw=e.clientX-resizeX,dh=e.clientY-resizeY;
    resizeWin.el.style.width=Math.max(360,resizeW+dw)+"px";
    resizeWin.el.style.height=Math.max(320,resizeH+dh)+"px";
  }
  function winResizeEnd(){
    resizeWin=null;
    document.removeEventListener("mousemove",winResizeMove);
    document.removeEventListener("mouseup",winResizeEnd);
  }

  // 渲染窗口标签栏（合并窗口显示为一条带子会话数的标签）
  function winTabsRender(){
    var html="";
    Object.keys(wins).forEach(function(k){
      var w=wins[k];
      var active=k===activeWinId?" active":"";
      var minimized=w.minimized?" (最小化)":"";
      var label=w.data.kind==="merged"?("合并窗口("+w.data.children.length+")"):w.data.title.slice(0,10);
      html+='<span class="dock-win-tab'+active+'" data-win="tab" data-id="'+k+'">'+escapeHtml(label)+minimized+'</span>';
    });
    winTabsEl.innerHTML=html;
    winTabsEl.querySelectorAll(".dock-win-tab").forEach(function(t){
      t.addEventListener("click",function(){
        var id=t.dataset.id;
        if(wins[id].minimized)winActivate(id);
        else if(activeWinId===id)winMinimize(id);
        else winActivate(id);
      });
    });
    // 合并按钮：有 >=2 个可见独立窗口时出现
    var mergeBtn=document.getElementById("merge-btn");
    if(mergeBtn){
      var standaloneN=Object.keys(wins).filter(function(k){return wins[k].data.kind!=="merged"&&!wins[k].minimized}).length;
      mergeBtn.style.display=standaloneN>=2?"":"none";
    }
  }

  // 新建聊天/任务窗口
  function newWin(kind){
    var title=kind==="chat"?"新对话":"新任务";
    var id="win_"+Date.now()+"_"+Math.random().toString(36).slice(2,8);
    winCreate(id,title,kind);
  }

  // 暴露给外部
  window.winCreate=winCreate;
  window.winCreateFromConv=function(id,title,kind){
    // 该会话已开在某窗口（独立或合并 tab）：激活即可
    var host=convHost[id];
    if(host&&wins[host]){
      if(wins[host].data.kind==="merged")activateMergedTab(host,id);
      winActivate(host);
      return;
    }
    winCreate(id,title,kind);
  };
  window.winAppendMsg=winAppendMsg;

  // 适配
  // API 基址：默认同源（页面由 8787 提供时即 http://127.0.0.1:8787）；
  // 可用 window.__MAESTRO_API__ 覆盖（测试/多实例部署）
  var MAESTRO=(window.__MAESTRO_API__||location.origin||"http://127.0.0.1:8787");
  var stage=document.getElementById("stage");
  function fit(){stage.style.setProperty("--s",Math.min(innerWidth/2560,innerHeight/1440))}
  fit(); addEventListener("resize",fit);

  // 视差：每个 .p 有 data-depth-x/y（或 --px/--py）作为 scene 视差系数
  // scene 视差正向=向观众（screen x=跟鼠标正方向；scene y 向上，CSS y 向下，所以 y 视差取反）
  var mx=0,my=0;
  addEventListener("mousemove",function(e){
    mx=(e.clientX/innerWidth-0.5)*2;
    my=(e.clientY/innerHeight-0.5)*2;
  });
  var PARALLAX=26;
  function applyParallax(){
    var els=stage.querySelectorAll(".p");
    for(var i=0;i<els.length;i++){
      var el=els[i];
      var px=parseFloat(el.dataset.depthX||el.style.getPropertyValue("--px"))||0;
      var py=parseFloat(el.dataset.depthY||el.style.getPropertyValue("--py"))||0;
      // 保留元素已有的 transform（scale/rotate）
      var baseT=el.dataset.baseT||"";
      // 这里用单独 transform：translate(px*mx*PARALLAX, py*my*PARALLAX) 叠加在原 transform 上
      // 视线模型：鼠标右→人物/画面朝右看（跟随鼠标，不取反）。
      // 之前取反（鼠标右→画面左）体验为"人物背对鼠标"。
      el.style.marginLeft=(px*mx*PARALLAX).toFixed(2)+"px";
      el.style.marginTop=(py*my*PARALLAX).toFixed(2)+"px";
    }
    requestAnimationFrame(applyParallax);
  }
  applyParallax();

  // 点击脸部彩蛋：眨眼 + 皱眉/嘟嘴表情切换 + 台词气泡
  var LINES=["嗯？需要帮忙吗？","新的任务来了哦。","我在看着你干活。","放心吧，交给编排器。"];
  var bubble=document.getElementById("bubble");
  var lastBlink=0;
  var hit=document.getElementById("hitface");
  function toggleExpr(showFrown) {
    // 表情层：嘟嘴/皱眉/嘴，按 src 识别（皱眉=qqqqqqqqqqqq.png，嘟嘴=aaaaaaaaaaaaaaazzz.png）
    document.querySelectorAll("img.p").forEach(function(el) {
      var src = el.getAttribute("src") || "";
      if (/aaaaaaaaaaaaaaazzz\.png/.test(src)) el.style.display = "none"; // 嘟嘴不用
      if (/qqqqqqqqqqqq\.png/.test(src)) el.style.display = showFrown ? "block" : "none";
      if (/嘴\.png/.test(src)) el.style.display = showFrown ? "none" : "block";
    });
  }
  if(hit)hit.addEventListener("click",function(e){
    var now=Date.now();
    if(now-lastBlink<600)return;
    lastBlink=now;
    // 眨眼：眼闭合
    document.querySelectorAll("img.eye").forEach(function(el){
      var t=el.style.transform;
      if(!/scaleY/.test(t))el.style.transform=t+" scaleY(0.12)";
      setTimeout(function(){el.style.transform=t},300);
    });
    // 皱眉：隐藏正常嘴，显示皱眉
    toggleExpr(true);
    setTimeout(function(){ toggleExpr(false); }, 1200);
    bubble.textContent=LINES[Math.floor(Math.random()*LINES.length)];
    bubble.style.display="block";
    clearTimeout(bubble._t);
    bubble._t=setTimeout(function(){bubble.style.display="none"},2600);
  });

  // 时钟（按 Clock 上/下 text 对象位置 + 日期）
  var clockEls=document.querySelectorAll(".txt");
  function tickClock(){
    var n=new Date();
    var hh=("00"+n.getHours()).slice(-2),mm=("00"+n.getMinutes()).slice(-2);
    var s=hh+":"+mm;
    var dateStr=n.getFullYear()+"/"+("00"+(n.getMonth()+1)).slice(-2)+"/"+("00"+n.getDate()).slice(-2);
    // 找 Clock下/Clock上/日期 text 节点（按内容）
    clockEls.forEach(function(el){
      var t=el.textContent;
      if(t==="HH:MM"||/^\d{2}:\d{2}$/.test(t))el.textContent=s;
      else if(/\d{4}/.test(t)&&/\d{2}.\d{2}.\d{2}/.test(t))el.textContent=dateStr;
      // Fri/eren/静态文本不动
    });
  }
  tickClock(); setInterval(tickClock,1000);

  // 特效 canvas：粒子（苍月草/雪/拖影） + 音条（composelayer 不渲染 DOM）
  var fx=document.getElementById("fx");
  var ctx=fx.getContext("2d");
  var fxb=document.getElementById("fx-bars");
  var ctxb=fxb.getContext("2d");
  function sizeFx(){fx.width=fxb.width=innerWidth;fx.height=fxb.height=innerHeight}
  sizeFx(); addEventListener("resize",sizeFx);

  // 下雪
  var SNOW=70,snow=[];
  for(var i=0;i<SNOW;i++)snow.push({x:Math.random(),y:Math.random(),s:0.6+Math.random()*1.8,v:0.0004+Math.random()*0.0012,w:Math.random()*2-1,a:0.3+Math.random()*0.5});
  // 拖影
  var trail=[];
  addEventListener("mousemove",function(e){trail.push({x:e.clientX,y:e.clientY,t:Date.now()});if(trail.length>40)trail.shift()});
  // 苍月草
  var blossoms=[];
  var blossomImg=new Image();
  blossomImg.src="/wallpaper/models/frieren/苍月草01.png";
  addEventListener("click",function(e){
    for(var i=0;i<6;i++)blossoms.push({x:e.clientX+(Math.random()-.5)*40,y:e.clientY+(Math.random()-.5)*30,vy:-0.6-Math.random()*0.8,vx:(Math.random()-.5)*0.7,rot:Math.random()*Math.PI*2,vr:(Math.random()-.5)*0.12,size:18+Math.random()*26,life:1});
  });
  // 音条数据（按 scene 收集）
  var audio=new Array(32).fill(0);
  if(window.wallpaperRegisterAudioListener){
    try{wallpaperRegisterAudioListener(function(levels){for(var i=0;i<32;i++)audio[i]=levels&&levels[i]?levels[i]:0})}catch(e){}
  }else{
    (function sim(){var t=Date.now()/1000;for(var i=0;i<32;i++)audio[i]=0.12+0.18*Math.abs(Math.sin(t*1.4+i*0.55))+0.1*Math.abs(Math.sin(t*2.3+i*0.9));setTimeout(sim,100)})();
  }
  function drawBars(c,cx,cy,angleDeg,w,h,colors){
    c.save();c.translate(cx,cy);c.rotate(angleDeg*Math.PI/180);
    var N=32,barW=w/N*0.55,gap=w/N;var x0=-w/2;
    for(var i=0;i<N;i++){var lv=audio[i]||0.08;var bh=Math.max(4,lv*h);var g=c.createLinearGradient(0,-bh/2,0,bh/2);g.addColorStop(0,colors[0]);g.addColorStop(1,colors[1]);c.fillStyle=g;c.fillRect(x0+i*gap,-bh/2,barW,bh)}
    c.restore();
  }
  function drawBarsLayer(){
    var W=fxb.width,H=fxb.height,s=Math.min(W/2560,H/1440);
    ctxb.clearRect(0,0,W,H);
    // 音条（用 scene [24][25] 的 css 坐标，按 1:1 缩放）——画在背景层（z-index 2），不遮挡芙莉莲
    drawBars(ctxb,1415*s,434*s,15.6,900*s,300*s,["rgba(180,185,235,0.85)","rgba(150,155,220,0.25)"]);
    drawBars(ctxb,1214*s,1010*s,15.6,900*s,300*s,["rgba(255,255,255,0.9)","rgba(220,225,255,0.3)"]);
    requestAnimationFrame(drawBarsLayer);
  }
  drawBarsLayer();
  function drawFx(){
    var W=fx.width,H=fx.height,s=Math.min(W/2560,H/1440);
    ctx.clearRect(0,0,W,H);
    // 下雪
    ctx.fillStyle="#fff";
    for(var k=0;k<SNOW;k++){
      var p=snow[k];p.y+=p.v;p.x+=Math.sin((Date.now()/1000)*0.8+k)*0.0004+p.w*0.00025;
      if(p.y>1){p.y=-0.02;p.x=Math.random()}
      if(p.x<-0.02)p.x=1.02;if(p.x>1.02)p.x=-0.02;
      ctx.globalAlpha=p.a;ctx.beginPath();ctx.arc(p.x*W,p.y*H,p.s,0,Math.PI*2);ctx.fill();
    }
    ctx.globalAlpha=1;
    // 拖影
    var now=Date.now();
    for(var t=0;t<trail.length;t++){var age=(now-trail[t].t)/500;if(age>1)continue;ctx.globalAlpha=(1-age)*0.3;ctx.fillStyle="#7a86cc";ctx.beginPath();ctx.arc(trail[t].x,trail[t].y,12*(1-age)+3,0,Math.PI*2);ctx.fill()}
    ctx.globalAlpha=1;
    // 苍月草
    if(blossomImg.complete&&blossomImg.naturalWidth>0){
      for(var b=blossoms.length-1;b>=0;b--){
        var bl=blossoms[b];bl.x+=bl.vx;bl.y+=bl.vy;bl.rot+=bl.vr;bl.life-=0.012;
        if(bl.life<=0){blossoms.splice(b,1);continue}
        ctx.save();ctx.globalAlpha=Math.max(0,bl.life);ctx.translate(bl.x,bl.y);ctx.rotate(bl.rot);
        var sz=bl.size*(0.5+0.5*bl.life);ctx.drawImage(blossomImg,-sz/2,-sz/2,sz,sz);ctx.restore();
      }
    }
    requestAnimationFrame(drawFx);
  }
  drawFx();

  // Maestro 状态
  // 状态面板数据源：/api/health/dashboard（每 5s 拉一次，与下方 poll 并存）
  var dashboardData={tasks:{},ws_connections:0,approvals_pending:0,recent_errors:[],degraded:false};
  function fmtRelTime(iso){
    if(!iso) return "";
    try{
      var t=new Date(iso).getTime();
      var dt=Date.now()-t;
      if(dt<60000) return Math.floor(dt/1000)+" 秒前";
      if(dt<3600000) return Math.floor(dt/60000)+" 分钟前";
      if(dt<86400000) return Math.floor(dt/3600000)+" 小时前";
      return Math.floor(dt/86400000)+" 天前";
    }catch(e){return iso;}
  }
  function renderDashboard(){
    var st=dashboardData.status||"ok";
    if(st==="degraded"){dot.className="m-dot failed";title.textContent="服务降级";}
    else if(st==="warn"){dot.className="m-dot running";title.textContent="需要注意";}
    // 摘要：运行/完成/失败/待审批/WS
    var running=dashboardData.tasks_running||0;
    var failed=dashboardData.tasks_failed||0;
    var ap=dashboardData.approvals_pending||0;
    var ws=dashboardData.ws_connections||0;
    var done=(dashboardData.tasks&&dashboardData.tasks.done)||0;
    var parts=[];
    parts.push("运行 "+running);
    parts.push("完成 "+done);
    parts.push("失败 "+failed);
    if(ap)parts.push("待审批 "+ap);
    if(ws>=0)parts.push("WS "+ws);
    sub.innerHTML=parts.join(" · ")+(dashboardData.degraded?'<div style="color:#d64545;font-size:11px">数据库查询异常（面板降级）</div>':"");
    off.style.display=st==="degraded"?"":"none";
    var html="";
    if(dashboardData.recent_errors&&dashboardData.recent_errors.length){
      html+='<div class="m-item" style="color:#d64545;font-weight:600">最近错误（24h）</div>';
      dashboardData.recent_errors.slice(0,5).forEach(function(e){
        html+='<div class="m-item" style="font-size:11px"><span style="color:#d64545">'+fmtRelTime(e.ts)+'</span> · '+escapeHtml(String(e.event||"").slice(0,40))+'</div>';
      });
    }
    var tks=dashboardData.tasks||{};
    var tnames={"pending":"待拆分","ready":"待确认","running":"运行中","done":"已完成","failed":"已失败","retry":"重试中","cancelled":"已取消"};
    Object.keys(tnames).forEach(function(k){
      if(tks[k]) html+='<div class="m-item"><span class="st '+(k==="done"?"ok":(k==="failed"?"fail":"run"))+'">'+tnames[k]+'</span>'+tks[k]+' 个任务</div>';
    });
    listEl.innerHTML=html||'<div class="m-item" style="color:#888">暂无任务 · 去 maestro web 面板发起</div>';
  }
  function fetchDashboard(){
    return fetch(MAESTRO+"/api/health/dashboard",{cache:"no-store"})
      .then(function(r){return r.ok?r.json():null})
      .then(function(d){if(d){dashboardData=d;renderDashboard();}})
      .catch(function(){});
  }

  var dot=document.getElementById("m-dot"),title=document.getElementById("m-title"),sub=document.getElementById("m-sub"),off=document.getElementById("m-off"),listEl=document.getElementById("m-list");
  function statusText(s){if(s==="done")return "已完成";if(s==="failed")return "失败";if(s==="running"||s==="pending")return "运行中";return s}
  function poll(){
    fetch(MAESTRO+"/api/tasks",{cache:"no-store"}).then(function(r){return r.json()}).then(function(tasks){
      off.style.display="none";var latest=tasks[0];
      var runn=tasks.filter(function(t){return t.status==="running"||t.status==="pending"}).length;
      var done=tasks.filter(function(t){return t.status==="done"}).length;
      var fail=tasks.filter(function(t){return t.status==="failed"}).length;
      if(latest&&(latest.status==="running"||latest.status==="pending")){
        dot.className="m-dot running";title.textContent="编排器运行中";
        sub.innerHTML="当前：<b>"+escapeHtml(latest.user_prompt)+"</b><br>任务 "+tasks.length+" · 运行 "+runn+" · 完成 "+done+" · 失败 "+fail;
      }else if(latest){
        dot.className="m-dot "+(latest.status==="done"?"done":"failed");
        title.textContent=latest.status==="done"?"编排器空闲":"最近任务失败";
        sub.innerHTML="最近："+statusText(latest.status)+" — <b>"+escapeHtml(latest.user_prompt)+"</b><br>任务 "+tasks.length+" · 完成 "+done+" · 失败 "+fail;
      }else{dot.className="m-dot";title.textContent="编排器就绪";sub.textContent="暂无任务 · 去 maestro web 面板发起"}
      var html="";tasks.slice(0,10).forEach(function(t){var cls=t.status==="done"?"ok":(t.status==="failed"?"fail":"run");html+='<div class="m-item"><span class="st '+cls+'">'+statusText(t.status)+"</span>"+escapeHtml(t.user_prompt)+"</div>"});
      listEl.innerHTML=html||'<div class="m-item" style="color:#888">暂无任务</div>';
    }).catch(function(){dot.className="m-dot";title.textContent="编排器未连接";sub.textContent="未找到 Maestro 服务（127.0.0.1:8787）";off.style.display="block"})
  }
  poll();setInterval(poll,3000);
  fetchDashboard();setInterval(fetchDashboard,5000);
  window.toggleList=function(e){e.stopPropagation();listEl.classList.toggle("show")};

  // ---------- 会话隔离（对话隔离） ----------
  function escapeHtml(s){return (s||"").replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
  var currentConv=null; // {id, title} 当前会话；null=未选择（输入框默认新建对话）
  // ========== 跨对话上下文关联 ==========
  // convLinks[convId] = [其他 convId,...]：发消息时随请求发给后端，注入对方最近几轮上下文
  var convLinks={};
  try{convLinks=JSON.parse(localStorage.getItem("maestro_conv_links")||"{}")||{}}catch(e){convLinks={}}
  function saveConvLinks(){try{localStorage.setItem("maestro_conv_links",JSON.stringify(convLinks))}catch(e){}}
  var convKind="chat";  // 当前 hover 列表显示的会话类型（跟随 dockMode）
  var convListEl=document.getElementById("conv-list");
  function refreshPlaceholder(){
    if(dockMode==="task"){dockInput.placeholder="交给编排器做什么（会新建对话）…";return}
    dockInput.placeholder = currentConv ? "和「"+currentConv.title+"」说点什么…" : "开始新对话…（或点「历史」选择已有对话）";
  }
  function setCurrentConv(id,title){
    currentConv={id:id,title:title};
    refreshPlaceholder();
    convListEl.querySelectorAll(".cv-item").forEach(function(el){
      el.classList.toggle("active",el.getAttribute("data-id")===id);
    });
  }
  function relTime(iso){
    if(!iso)return "";
    var t=new Date(iso);
    if(isNaN(t.getTime()))return "";
    var diff=(Date.now()-t.getTime())/1000;
    if(diff<60)return "刚刚";
    if(diff<3600)return Math.floor(diff/60)+" 分钟前";
    if(diff<86400)return Math.floor(diff/3600)+" 小时前";
    if(diff<86400*7)return Math.floor(diff/86400)+" 天前";
    return (t.getMonth()+1)+"-"+t.getDate();
  }
  function renderConvItem(c){
    var active=(currentConv&&currentConv.id===c.id)?" active":"";
    var prev=(c.last_content||"").slice(0,28)||"暂无消息";
    return '<div class="cv-item'+active+'" data-id="'+c.id+'" data-kind="'+(c.kind||"chat")+'" data-title="'+escapeHtml(c.title)+'">'+
      '<div class="cv-title"><span class="cv-name" title="'+escapeHtml(c.title)+'">'+escapeHtml(c.title||"")+'</span>'+
        '<span class="cv-ops">'+
          '<button class="cv-op" data-act="rename" title="重命名">改名</button>'+
          '<button class="cv-op" data-act="export" title="导出为 Markdown">导出</button>'+
          '<button class="cv-op del" data-act="del" title="删除">删除</button>'+
        '</span></div>'+
      '<div class="cv-prev">'+escapeHtml(prev)+' · '+c.msg_count+'条 · '+relTime(c.updated_at)+'</div></div>';
  }
  function convDelete(id){
    if(!confirm("确定删除这个对话吗？删除后不可恢复。"))return;
    fetch(MAESTRO+"/api/conversations/"+encodeURIComponent(id),{method:"DELETE"})
      .then(function(r){return r.json()})
      .then(function(){
        if(currentConv&&currentConv.id===id){currentConv=null;refreshPlaceholder()}
        loadConvs();
      })
      .catch(function(){alert("删除失败，请重试")});
  }
  function convRename(id,item){
    var title=prompt("输入新名称：",item.getAttribute("data-title"));
    if(title===null)return;
    title=title.trim();
    if(!title)return;
    fetch(MAESTRO+"/api/conversations/"+encodeURIComponent(id),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title})})
      .then(function(r){return r.json()})
      .then(function(){
        if(currentConv&&currentConv.id===id)setCurrentConv(id,title);
        loadConvs();
      })
      .catch(function(){alert("重命名失败，请重试")});
  }
  function convExport(id){
    window.open(MAESTRO+"/api/conversations/"+encodeURIComponent(id)+"/export","_blank");
  }
  var convSearchQ="";
  function loadConvs(kind){
    kind=kind||convKind;
    var url=MAESTRO+"/api/conversations?kind="+kind+
      (convSearchQ?"&q="+encodeURIComponent(convSearchQ):"");
    return fetch(url,{cache:"no-store"})
      .then(function(r){return r.json()})
      .then(function(d){
        var list=d.conversations||[];
        var newLabel=kind==="task"?"＋ 新建任务对话":"＋ 新建对话";
        convListEl.innerHTML='<div class="cv-search"><input id="cv-search" placeholder="搜索标题或消息内容…" value="'+escapeHtml(convSearchQ)+'" /></div>'+
          '<div class="cv-new" id="cv-new">'+newLabel+'</div>'+
          (list.map(renderConvItem).join("")||('<div class="cv-empty">'+(convSearchQ?"无匹配会话":"暂无会话")+'</div>'));
        bindConvSearch();
        return list;
      })
      .catch(function(){convListEl.innerHTML='<div class="cv-empty">未连接编排器</div>'});
  }
  // 搜索框事件（列表每次重渲染后重新绑定；300ms 防抖，Esc 清空）
  function bindConvSearch(){
    var inp=document.getElementById("cv-search");
    if(!inp)return;
    var timer=null;
    inp.addEventListener("input",function(){
      clearTimeout(timer);
      timer=setTimeout(function(){
        convSearchQ=inp.value.trim();
        loadConvs();
        // 重渲染后焦点回到搜索框、光标到末尾
        var el2=document.getElementById("cv-search");
        if(el2){el2.focus();el2.setSelectionRange(el2.value.length,el2.value.length);}
      },300);
    });
    inp.addEventListener("keydown",function(e){
      if(e.key==="Escape"){convSearchQ="";loadConvs();}
    });
  }
  function createNewConv(title,kind){
    return fetch(MAESTRO+"/api/conversations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title||"新对话",kind:kind||convKind})})
      .then(function(r){return r.json()})
      .then(function(d){
        setCurrentConv(d.conv_id,d.title);
        loadConvs(d.kind);
        return d;
      });
  }
  // 点击会话 → 创建/激活窗口；点「＋ 新建对话」→ 创建新会话；点操作按钮 → 管理会话
  convListEl.addEventListener("click",function(e){
    var op=e.target.closest(".cv-op");
    if(op){
      e.stopPropagation();
      var item=op.closest(".cv-item");
      var id=item.getAttribute("data-id");
      var act=op.getAttribute("data-act");
      if(act==="del")convDelete(id);
      else if(act==="rename")convRename(id,item);
      else if(act==="export")convExport(id);
      return;
    }
    if(e.target.closest("#cv-new")){
      createNewConv("新对话",convKind).then(function(d){
        winCreateFromConv(d.conv_id,d.title,d.kind);
      });
      return;
    }
    var item=e.target.closest(".cv-item");
    if(!item)return;
    var id=item.getAttribute("data-id"),title=item.getAttribute("data-title"),kind=item.getAttribute("data-kind");
    // 创建或激活窗口
    winCreateFromConv(id,title,kind);
  });

  // 底部输入框：聊天 / 派任务 双模式
  var dockInput=document.getElementById("dock-input");
  var dockSend=document.getElementById("dock-send");
  var reply=document.getElementById("reply");
  var dockMode="chat";
  var dockTabs=document.querySelectorAll(".dock-tab");
  function setMode(m){
    dockMode=m;
    convKind=m; // 聊天/任务历史分开：hover 显示对应类型会话
    for(var i=0;i<dockTabs.length;i++)dockTabs[i].className="dock-tab"+(dockTabs[i].getAttribute("data-mode")===m?" active":"");
    refreshPlaceholder();
    loadConvs(m);
    winSelectRender(); // 更新窗口选择器的"新建"选项
    document.getElementById("agent-selector").className="agent-selector"+(m==="task"?" visible":"");
    document.getElementById("wf-row").style.display=m==="task"?"flex":"none";
    dockInput.focus();
  }
  for(var i=0;i<dockTabs.length;i++)(function(t){t.addEventListener("click",function(){setMode(t.getAttribute("data-mode"))})})(dockTabs[i]);
  function showReply(txt,keep){
    reply.textContent=txt;reply.style.display="block";
    clearTimeout(reply._t);
    reply._t=setTimeout(function(){reply.style.display="none"},keep||6000);
  }
  var lastSendTime=0;
  var sendThrottle=1500; // 发送间隔1.5秒
  function dockSendAction(){
    var v=dockInput.value.trim();
    if(!v)return;
    // 节流检查
    var now=Date.now();
    if(now-lastSendTime<sendThrottle){
      showReply("发送太频繁，请稍后再试",2000);
      return;
    }
    lastSendTime=now;
    var targetId=selectedWinId; // 当前选中的会话（独立窗口或合并窗口里的 tab）
    // 检查是否需要先创建新窗口和会话
    if(pendingNewKind || !targetId || !winOfConv(targetId)){
      // 先创建真正的会话
      var title=pendingNewKind==="task"?"新任务":"新对话";
      fetch(MAESTRO+"/api/conversations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title,kind:pendingNewKind||"chat"})})
        .then(function(r){return r.json()})
        .then(function(d){
          var realConvId=d.conv_id;
          var newId=realConvId; // 用真实会话 ID 作为窗口 ID
          winCreate(newId,title,pendingNewKind||"chat");
          selectedWinId=newId;
          pendingNewKind=null;
          winSelectRender();
          // 发送消息
          doSend(newId,v);
        })
        .catch(function(){alert("创建会话失败")});
      resetDockInput();
      return;
    }
    resetDockInput();
    // 发送到选中的窗口
    doSend(targetId,v);
  }
  function doSend(targetId,v,retryCount){
    // targetId 是会话 id（独立窗口与自身同 id；合并窗口中为当前 tab 的会话）
    if(!targetId || !convMeta[targetId] || !winOfConv(targetId)){dockInput.value=v;return}
    retryCount=retryCount||0;
    var kind=convMeta[targetId].kind;
    winAppendMsg(targetId,"user",v);
    if(kind==="chat")showTyping(targetId); // 芙莉莲正在输入…
    if(kind==="chat"){
      // 聊天模式：流式回复（SSE）
      fetch(MAESTRO+"/api/chat/stream",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:v,conv_id:targetId,link_conv_ids:(convLinks[targetId]||[])})})
        .then(function(resp){
          if(!resp.ok||!resp.body)throw new Error("http "+resp.status);
          var reader=resp.body.getReader(),dec=new TextDecoder(),buf="";
          var visible="";
          function pump(){
            return reader.read().then(function(x){
              if(x.done){
                // 流结束：去流式光标 + 长回复折叠
                var wb=msgsEl(targetId);
                var lastDone=wb&&wb.lastElementChild;
                if(lastDone&&lastDone.classList.contains("assistant")&&!lastDone.classList.contains("typing")){
                  lastDone.dataset.text=visible;
                  var cDone=lastDone.querySelector(".msg-content");
                  if(cDone)cDone.innerHTML=mdRender(visible);
                  applyFold(lastDone,visible);
                }
                hideTyping(targetId);
                return;
              }
              buf+=dec.decode(x.value,{stream:true});
              var parts=buf.split("\n\n");
              buf=parts.pop();
              for(var i=0;i<parts.length;i++){
                var line=parts[i].trim();
                if(line.indexOf("data:")!==0)continue;
                try{
                  var d=JSON.parse(line.slice(5).trim());
                  if(d.delta)visible+=d.delta;
                }catch(e){}
              }
              // 更新最后一条消息（打字指示气泡就地转为正文，只重写 .msg-content）
              var body=msgsEl(targetId);
              if(!body)return;
              var lastMsg=body.lastElementChild;
              if(lastMsg&&lastMsg.classList.contains("typing")){
                lastMsg.classList.remove("typing");
                lastMsg.dataset.text="";
                lastMsg.innerHTML=AVATAR_HTML+'<div class="msg-content"></div><div class="msg-meta"><span class="win-time">'+new Date().toLocaleTimeString()+"</span></div>";
              }
              lastMsg=body.lastElementChild;
              if(lastMsg&&lastMsg.classList.contains("assistant")){
                lastMsg.dataset.text=visible;
                var cEl=lastMsg.querySelector(".msg-content");
                if(cEl)cEl.innerHTML=mdRender(visible)+'<span class="caret"></span>';
                else lastMsg.innerHTML=escapeHtml(visible)+'<div class="win-time">'+new Date().toLocaleTimeString()+"</div>";
              }else{
                winAppendMsg(targetId,"assistant",visible);
              }
              body.scrollTop=body.scrollHeight;
              return pump();
            });
          }
          return pump();
        })
        .catch(function(e){
          console.error("chat error:",e);
          hideTyping(targetId);
          // 网络错误时重试
          if(retryCount<2&&(e.message.includes("Failed to fetch")||e.message.includes("network")||e.message.includes("fetch"))){
            winAppendMsg(targetId,"assistant","网络不稳定，正在重试...");
            setTimeout(function(){doSend(targetId,v,retryCount+1)},1500);
          }else{
            winAppendMsg(targetId,"assistant","连接失败: "+e.message+"（点击消息重试）");
            // 点击失败消息重试
            var body=msgsEl(targetId);
            if(body){
              var lastMsg=body.lastElementChild;
              if(lastMsg&&lastMsg.textContent.includes("连接失败")){
                lastMsg.style.cursor="pointer";
                lastMsg.onclick=function(){doSend(targetId,v,0);lastMsg.onclick=null;lastMsg.textContent="重试中...";};
              }
            }
          }
        });
    }else{
      // 任务模式：派发任务（选中工作流 → 服务端按预设/卡片展开；未选中 → auto）
      var selectedAgents=Array.from(document.querySelectorAll("#agent-chips .agent-chip.selected"))
        .map(function(ch){return ch.dataset.name});
      if(selectedAgents.length===0)selectedAgents=["embedded"];
      var selectedModel=document.getElementById("model-select").value||null;
      var wfSel=document.getElementById("workflow-select");
      var wfId=(wfSel&&wfSel.value)||null;
      var wfLabel=(wfSel&&wfSel.selectedIndex>=0)?wfSel.options[wfSel.selectedIndex].text.split(" ")[0]:"";
      var reqBody={prompt:v,conv_id:targetId,workflow:wfId,selected_workers:selectedAgents,model:selectedModel};
      if(!wfId){reqBody.scenario="auto";reqBody.confirm=false;}
      fetch(MAESTRO+"/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(reqBody)})
        .then(function(r){return r.json()})
        .then(function(d){
          var taskId=d.task_id;
          var agents=d.selected_workers&&d.selected_workers.length>0?d.selected_workers:["embedded"];
          if(d.workflow&&wfLabel)agents=[wfLabel].concat(agents);
          if(taskId){
            // 富任务卡片：状态/进度/子任务/结果自带轮询
            winAppendTaskCard(targetId,taskId,agents,d.model||null,v);
          }else{
            winAppendMsg(targetId,"assistant","任务提交异常："+(d.error||"无 task_id"));
          }
          poll(); // 刷新任务列表
        })
        .catch(function(){winAppendMsg(targetId,"assistant","任务提交失败");});
    }
  }
  // 模式切换（聊天/任务）
    document.getElementById("mode-chat").addEventListener("click",function(){setMode("chat")});
    document.getElementById("mode-task").addEventListener("click",function(){setMode("task")});
    // 加载 agent 列表
    function loadAgents(){
      fetch(MAESTRO+"/api/agents").then(function(r){return r.json()}).then(function(agents){
        var chipsEl=document.getElementById("agent-chips");
        chipsEl.innerHTML="";
        agents.forEach(function(a){
          var chip=document.createElement("label");
          chip.className="agent-chip"+(a.available?"":" disabled");
          chip.dataset.name=a.name;
          chip.title=a.desc+(a.available?"":"（未连接）");
          chip.innerHTML='<input type="checkbox" value="'+escapeHtml(a.name)+'" />'+
            '<span class="chip-check"></span>'+
            '<span>'+escapeHtml(a.label)+'</span>'+
            '<span class="chip-dot"></span>';
          chip.addEventListener("click",function(e){
            if(chip.classList.contains("disabled"))return;
            e.preventDefault();
            chip.classList.toggle("selected");
            var cb=chip.querySelector("input");
            cb.checked=chip.classList.contains("selected");
          });
          chipsEl.appendChild(chip);
          if(a.name==="embedded"&&a.available){
            chip.classList.add("selected");
            chip.querySelector("input").checked=true;
          }
        });
      }).catch(function(){});
    }
    loadAgents();
    // agent 列表同时喂给工作流编排器（卡片配置下拉用）
    fetch(MAESTRO+"/api/agents").then(function(r){return r.json()}).then(function(list){
      if(window.__setWfAgents)window.__setWfAgents(list||[]);
    }).catch(function(){});
    // 加载模型列表
    var _modelsCache=null;
    function loadModels(){
      fetch(MAESTRO+"/api/models").then(function(r){return r.json()}).then(function(models){
        _modelsCache=models;
        var sel=document.getElementById("model-select");
        sel.innerHTML="";
        var grouped={};
        models.forEach(function(m){
          var prov=m.provider||"other";
          if(!grouped[prov])grouped[prov]={label:prov,models:[]};
          grouped[prov].models.push(m);
        });
        var row=document.getElementById("model-row");
        if(models.length===0){row.style.display="none";return;}
        row.style.display="flex";
        Object.keys(grouped).forEach(function(prov){
          var optgroup=document.createElement("optgroup");
          optgroup.label=grouped[prov].label;
          grouped[prov].models.forEach(function(m){
            var o=document.createElement("option");
            o.value=m.id;
            o.textContent=m.label+" — "+m.desc;
            o.title=m.key_label||"";
            optgroup.appendChild(o);
          });
          sel.appendChild(optgroup);
        });
        if(models.length>0)sel.value=models[0].id;
      }).catch(function(){document.getElementById("model-row").style.display="none"});
    }
    loadModels();

  // ========== 工作流：选择器 + 可视化卡片编排器 ==========
  var wfLibCache=[];   // /api/workflows（preset+user）
  var wfSkillLib={};   // name -> {category,snippet}
  var wfAgentsCache=[];// /api/agents
  var convWf={};       // convId -> workflowId（每会话记忆）
  if(!window.__setWfAgents)window.__setWfAgents=function(list){wfAgentsCache=list||[]};
  try{convWf=JSON.parse(localStorage.getItem("maestro_conv_wf")||"{}")||{}}catch(e){convWf={}}
  var WF_COLORS=["#5b8def","#34c98e","#f0a24b","#a06bf0","#e0679a","#43bcd6","#8b9c68","#d67171"];
  var SKILL_COLORS={"设计":"#e0679a","代码":"#5b8def","文档":"#34c98e","视频":"#f0a24b",
    "音频":"#a06bf0","数据":"#43bcd6","图像":"#8b9c68","网络":"#d67171","通用":"#9aa3b8"};
  var wfState={stages:[]};
  var wfEditingId=null;
  var wfBuilderOpen=false;

  function saveConvWf(){try{localStorage.setItem("maestro_conv_wf",JSON.stringify(convWf))}catch(e){}}

  function loadWorkflows(){
    return fetch(MAESTRO+"/api/workflows").then(function(r){return r.json()}).then(function(list){
      wfLibCache=list||[];
      var sel=document.getElementById("workflow-select");
      if(!sel)return wfLibCache;
      var cur=sel.value;
      var htmlOpt="";
      var presets=wfLibCache.filter(function(w){return w.source==="preset"});
      var users=wfLibCache.filter(function(w){return w.source==="user"});
      if(presets.length){
        htmlOpt+='<optgroup label="预设">';
        presets.forEach(function(w){htmlOpt+='<option value="'+w.id+'">'+w.icon+" "+w.name+'</option>';});
        htmlOpt+="</optgroup>";
      }
      if(users.length){
        htmlOpt+='<optgroup label="我的工作流">';
        users.forEach(function(w){htmlOpt+='<option value="'+w.id+'">🧩 '+escapeHtml(w.name)+'</option>';});
        htmlOpt+="</optgroup>";
      }
      sel.innerHTML=htmlOpt;
      // 恢复当前会话的记忆；否则默认标准编排
      var saved=selectedWinId&&convWf[selectedWinId];
      if(saved&&sel.querySelector('option[value="'+saved+'"]'))sel.value=saved;
      else if(sel.querySelector('option[value="standard"]'))sel.value="standard";
      return wfLibCache;
    }).catch(function(){return []});
  }

  function loadSkillLib(){
    return fetch(MAESTRO+"/api/skills").then(function(r){return r.json()}).then(function(list){
      wfSkillLib={};
      (list||[]).forEach(function(s){wfSkillLib[s.name]={category:s.category,snippet:s.snippet,id:s.id,source:s.source};});
      return wfSkillLib;
    }).catch(function(){return {}});
  }

  // 工作流选择变化：记到当前会话 + 预设默认铺到 agent chips / 模型
  function onWorkflowChange(){
    var sel=document.getElementById("workflow-select");
    if(selectedWinId){convWf[selectedWinId]=sel.value||null;saveConvWf();}
    var wf=wfLibCache.filter(function(w){return w.id===sel.value})[0];
    if(wf&&wf.source==="preset"){
      document.querySelectorAll("#agent-chips .agent-chip").forEach(function(chip){
        var want=wf.workers.indexOf(chip.dataset.name)>=0&&!chip.classList.contains("disabled");
        chip.classList.toggle("selected",want);
        var cb=chip.querySelector("input");if(cb)cb.checked=want;
      });
      if(wf.model){
        var ms=document.getElementById("model-select");
        if(ms&&ms.querySelector('option[value="'+wf.model+'"]'))ms.value=wf.model;
      }
    }
  }

  // ===== 可视化编排器 =====
  function wfNewCard(){
    return {id:"card_"+Date.now()+"_"+Math.random().toString(36).slice(2,6),
      enabled:true,desc:"",worker:"embedded",model:null,skills:[]};
  }
  function wfOpen(){
    var b=document.getElementById("wf-builder");
    b.classList.add("show");
    wfBuilderOpen=true;
    loadSkillLib();
    if(!wfState.stages.length){
      // 空骨架：两个环节各一张卡
      wfState.stages=[
        {name:"环节 1",cards:[wfNewCard()]},
        {name:"环节 2",cards:[wfNewCard()]}
      ];
      wfState.stages[0].cards[0].desc="负责什么任务（例如：收集资料并整理要点）";
    }
    // 载入下拉：预设 + 已保存
    var load=document.getElementById("wfb-load");
    var opts='<option value="">— 选择模板或已保存的工作流 —</option>';
    wfLibCache.forEach(function(w){
      opts+='<option value="'+w.id+'">'+(w.source==="user"?"🧩 ":"")+w.icon+" "+escapeHtml(w.name)+'</option>';
    });
    load.innerHTML=opts;
    renderWfBuilder();
  }
  function closeWfBuilder(){
    document.getElementById("wf-builder").classList.remove("show");
    wfBuilderOpen=false;
  }
  function renderWfBuilder(){
    var wrap=document.getElementById("wfb-stages");
    wrap.innerHTML="";
    wfState.stages.forEach(function(st,si){
      var color=WF_COLORS[si%WF_COLORS.length];
      var stageEl=document.createElement("div");
      stageEl.className="wfb-stage";
      stageEl.style.setProperty("--stc",color);
      var head=document.createElement("div");
      head.className="wfb-stage-head";
      head.innerHTML='<input value="'+escapeHtml(st.name)+'" placeholder="环节名" data-si="'+si+'">'+
        '<button class="wfb-stage-del" data-si="'+si+'" title="删除环节">×</button>';
      var cardsEl=document.createElement("div");
      cardsEl.className="wfb-stage-cards";
      st.cards.forEach(function(c,ci){cardsEl.appendChild(wfCardEl(si,ci,color));});
      var addBtn=document.createElement("button");
      addBtn.className="wfb-addcard";
      addBtn.type="button";
      addBtn.textContent="＋ 添加卡片";
      addBtn.addEventListener("click",function(){
        st.cards.push(wfNewCard());
        renderWfBuilder();
      });
      cardsEl.appendChild(addBtn);
      stageEl.appendChild(head);
      stageEl.appendChild(cardsEl);
      wrap.appendChild(stageEl);
    });
    // 事件绑定（重建后）
    wrap.querySelectorAll(".wfb-stage-head input").forEach(function(inp){
      inp.addEventListener("input",function(){wfState.stages[+inp.dataset.si].name=inp.value;});
    });
    wrap.querySelectorAll(".wfb-stage-del").forEach(function(btn){
      btn.addEventListener("click",function(){
        var si=+btn.dataset.si;
        if(wfState.stages.length<=1){showReply("至少保留一个环节",1800);return;}
        wfState.stages.splice(si,1);
        renderWfBuilder();
      });
    });
  }
  function wfCardEl(si,ci,color){
    var c=wfState.stages[si].cards[ci];
    var card=document.createElement("div");
    card.className="wf-card "+(c.enabled?"on":"off")+(c._open?" configuring":"");
    card.dataset.si=si;card.dataset.ci=ci;
    card.style.setProperty("--stc",color);
    var skillHtml=(c.skills||[]).map(function(sn){
      var cat=wfSkillLib[sn]&&wfSkillLib[sn].category||"通用";
      return '<span class="wc-skill skill-chip" data-rmskill="'+escapeHtml(sn)+'" style="background:'+(SKILL_COLORS[cat]||SKILL_COLORS["通用"])+'" title="点击移除">'+escapeHtml(sn)+'</span>';
    }).join("");
    var workerOpts="";
    wfAgentsCache.forEach(function(a){
      workerOpts+='<option value="'+a.name+'"'+(c.worker===a.name?" selected":"")+'>'+a.label+'</option>';
    });
    var modelOpts='<option value="">默认模型</option>';
    (window.__modelsCache||[]).forEach(function(m){
      modelOpts+='<option value="'+m.id+'"'+(c.model===m.id?" selected":"")+'>'+escapeHtml(m.label)+'</option>';
    });
    card.innerHTML=
      '<div class="wc-title">'+(c.desc?escapeHtml(c.desc):'<span style="color:#b6bcd8">（未填写任务）</span>')+'</div>'+
      '<div class="wc-meta"><span>'+escapeHtml(wfAgentLabel(c.worker))+'</span>'+
        (c.model?"<span>· "+escapeHtml(c.model)+"</span>":"")+(skillHtml?"<span>·</span>"+skillHtml:"")+'</div>'+
      '<div class="wc-ops">'+
        '<button class="wc-set" type="button" title="配置">⚙</button>'+
        '<button class="wc-del" type="button" title="删除卡片">×</button>'+
      '</div>'+
      '<div class="wc-config">'+
        '<div class="wc-lrow"><span class="wl">智能体</span><select class="wc-worker">'+workerOpts+'</select>'+
          '<span class="wl">模型</span><select class="wc-model">'+modelOpts+'</select></div>'+
        '<div class="wc-lrow" style="margin-top:5px"><label style="display:flex;gap:5px;align-items:center;cursor:pointer">'+
          '<input type="checkbox" class="wc-prev"'+(c.use_prev?" checked":"")+(si===0?" disabled":"")+'>'+
          '<span style="font-size:11px">引用上一环节产出（把上一环节结果喂给这张卡片）</span></label></div>'+
        '<textarea class="wc-desc" placeholder="这张卡片负责什么任务（会与你的主输入合并）">'+escapeHtml(c.desc)+'</textarea>'+
        '<div class="skill-chips">'+(c.skills&&c.skills.length?"":"<span style='color:#b6bcd8;font-size:10px'>尚未选择技能</span>")+'</div>'+
        '<div class="skill-add"><input placeholder="输入技能名，回车自动分类"><button type="button">添加</button></div>'+
      '</div>';
    // —— 交互 ——
    card.addEventListener("click",function(e){
      if(e.target.closest(".wc-ops")||e.target.closest(".wc-config")||e.target.closest(".skill-chip"))return;
      c.enabled=!c.enabled;
      card.classList.toggle("on",c.enabled);
      card.classList.toggle("off",!c.enabled);
    });
    card.querySelector(".wc-set").addEventListener("click",function(e){
      e.stopPropagation();
      c._open=!c._open;
      card.classList.toggle("configuring",c._open);
    });
    card.querySelector(".wc-del").addEventListener("click",function(e){
      e.stopPropagation();
      wfState.stages[si].cards.splice(ci,1);
      renderWfBuilder();
    });
    var ta=card.querySelector(".wc-desc");
    ta.addEventListener("input",function(){
      c.desc=ta.value;
      card.querySelector(".wc-title").textContent=c.desc||"（未填写任务）";
    });
    card.querySelector(".wc-worker").addEventListener("change",function(e){c.worker=e.target.value;updateCardMeta();});
    card.querySelector(".wc-model").addEventListener("change",function(e){c.model=e.target.value||null;updateCardMeta();});
    var prevCb=card.querySelector(".wc-prev");
    if(prevCb)prevCb.addEventListener("change",function(){c.use_prev=prevCb.checked;});
    function updateCardMeta(){
      var meta=card.querySelector(".wc-meta");
      meta.innerHTML='<span>'+escapeHtml(wfAgentLabel(c.worker))+'</span>'+
        (c.model?"<span>· "+escapeHtml(c.model)+"</span>":"")+
        ((c.skills||[]).length?"<span>·</span>":"");
      (c.skills||[]).forEach(function(sn){
        var cat=wfSkillLib[sn]&&wfSkillLib[sn].category||"通用";
        var sp=document.createElement("span");
        sp.className="wc-skill skill-chip";
        sp.style.background=SKILL_COLORS[cat]||SKILL_COLORS["通用"];
        sp.title="点击移除";
        sp.textContent=sn;
        sp.dataset.rmskill=sn;
        meta.appendChild(sp);
      });
    }
    card.querySelectorAll(".skill-chip[data-rmskill]").forEach(function(ch){
      ch.addEventListener("click",function(e){
        e.stopPropagation();
        var name=ch.dataset.rmskill;
        c.skills=(c.skills||[]).filter(function(x){return x!==name;});
        renderWfBuilder();
      });
    });
    var addBtn=card.querySelector(".skill-add button");
    var addInput=card.querySelector(".skill-add input");
    function doAdd(){
      var name=(addInput.value||"").trim();
      if(!name)return;
      if((c.skills||[]).indexOf(name)>=0){addInput.value="";return;}
      fetch(MAESTRO+"/api/skills",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name:name})})
        .then(function(r){return r.json()})
        .then(function(s){
          if(!s||!s.name){showReply("技能添加失败："+((s&&s.detail)||""),2500);return;}
          wfSkillLib[s.name]={category:s.category,snippet:s.snippet,id:s.id,source:s.source};
          c.skills=c.skills||[];
          if(c.skills.indexOf(s.name)<0)c.skills.push(s.name);
          renderWfBuilder();
        })
        .catch(function(){showReply("技能添加失败：服务未连接",2500)});
    }
    addBtn.addEventListener("click",doAdd);
    addInput.addEventListener("keydown",function(e){if(e.key==="Enter"){e.preventDefault();doAdd();}});
    return card;
  }
  function wfAgentLabel(name){
    var a=wfAgentsCache.filter(function(x){return x.name===name})[0];
    return a?a.label:(name||"embedded");
  }
  function wfCollectSubtasks(){
    var subs=[];
    wfState.stages.forEach(function(st,si){
      st.cards.forEach(function(c){
        if(!c.enabled)return;
        var desc=(c.desc||"").trim();
        if(!desc)return;
        subs.push({desc:"【"+st.name+"】"+desc,worker_type:c.worker||"embedded",
          model:c.model||null,skills:(c.skills||[]).slice(),
          stage:si,use_prev:!!c.use_prev&&si>0});
      });
    });
    return subs;
  }
  // ===== 运行视图：编排器面板内实时显示各环节卡片状态/产出 =====
  var wfRunView={taskId:null,convId:null,timer:0};
  function wfRunViewStart(convId,taskId){
    wfRunView={taskId:taskId,convId:convId,timer:0};
    var b=document.getElementById("wf-builder");
    b.classList.add("running");
    var hd=b.querySelector(".wfb-head .wfb-btn#run-btn")||b.querySelector(".wfb-head");
    // 顶部提示条
    var tip=b.querySelector(".wfb-run-tip");
    if(!tip){
      tip=document.createElement("div");
      tip.className="wfb-run-tip";
      var hint=b.querySelector(".wfb-hint");
      hint.parentNode.insertBefore(tip,hint.nextSibling);
    }
    tip.innerHTML='<span class="rt-dot"></span>运行中 · <b>'+escapeHtml(taskId)+'</b>（卡片右上角显示实时状态，点 ▐▐ 关闭跟踪）';
    var stop=document.createElement("button");
    stop.type="button";stop.className="rt-stop";stop.textContent="▐▐ 关闭跟踪";
    stop.addEventListener("click",function(){wfRunViewStop();});
    tip.appendChild(stop);
    wfRunViewTick();
  }
  function wfRunViewStop(){
    clearTimeout(wfRunView.timer);
    wfRunView={taskId:null,convId:null,timer:0};
    var b=document.getElementById("wf-builder");
    b.classList.remove("running");
    var tip=b.querySelector(".wfb-run-tip");
    if(tip)tip.remove();
    // 清掉卡片上的运行徽章
    b.querySelectorAll(".wf-card .wc-run").forEach(function(el){el.remove();});
    b.querySelectorAll(".wf-card.wc-running,.wf-card.wc-done,.wf-card.wc-failed").forEach(function(el){
      el.classList.remove("wc-running","wc-done","wc-failed");
    });
  }
  function wfRunViewTick(){
    if(!wfRunView.taskId)return;
    fetch(MAESTRO+"/api/tasks/"+encodeURIComponent(wfRunView.taskId),{cache:"no-store"})
      .then(function(r){return r.ok?r.json():null})
      .then(function(snap){
        if(!snap||!snap.task){wfRunViewStop();return;}
        wfRunViewRender(snap);
        if(snap.task.status==="done"||snap.task.status==="failed"){
          var tip=document.querySelector(".wfb-run-tip .rt-dot");
          if(tip)tip.classList.add(snap.task.status==="done"?"ok":"bad");
          var txt=document.querySelector(".wfb-run-tip");
          if(txt)txt.childNodes[1].textContent=snap.task.status==="done"?"已完成 ✔ ":"已失败 ✖ ";
          return; // 终态：停止轮询（保留徽章供查看）
        }
        wfRunView.timer=setTimeout(wfRunViewTick,1500);
      })
      .catch(function(){wfRunView.timer=setTimeout(wfRunViewTick,4000)});
  }
  function wfRunViewRender(snap){
    // 子任务按 idx 对应回编排器卡片：提交顺序 = stage 分组展开顺序 = wfCollectSubtasks 顺序
    var subs=snap.subtasks||[];
    var flat=[]; // [{si,ci}]
    wfState.stages.forEach(function(st,si){
      st.cards.forEach(function(c,ci){ if(c.enabled&&((c.desc||"").trim())) flat.push({si:si,ci:ci}); });
    });
    var b=document.getElementById("wf-builder");
    subs.forEach(function(s,i){
      var pos=flat[i];
      if(!pos)return;
      var card=b.querySelector('.wf-card[data-si="'+pos.si+'"][data-ci="'+pos.ci+'"]');
      if(!card)return;
      var badge=card.querySelector(".wc-run");
      if(!badge){
        badge=document.createElement("span");
        badge.className="wc-run";
        card.querySelector(".wc-ops").appendChild(badge);
      }
      var map={pending:"待执行",running:"执行中",done:"完成",failed:"失败",cancelled:"跳过"};
      badge.textContent=map[s.status]||s.status;
      badge.className="wc-run st-"+s.status;
      card.classList.toggle("wc-running",s.status==="running");
      card.classList.toggle("wc-done",s.status==="done");
      card.classList.toggle("wc-failed",s.status==="failed");
      // 产出预览（title 提示 + 完成时前 60 字内联展示）
      var prev=card.querySelector(".wc-output");
      if(s.status==="done"&&s.output){
        if(!prev){
          prev=document.createElement("div");
          prev.className="wc-output";
          card.appendChild(prev);
        }
        var out=String(s.output).slice(0,80);
        prev.textContent=out+(String(s.output).length>80?"…":"");
        prev.title=String(s.output).slice(0,500);
      }else if(prev&&s.status==="running"){
        prev.remove();
      }
      if(s.error){
        badge.title=String(s.error).slice(0,300);
      }
    });
  }
  function wfSave(){
    var name=document.getElementById("wfb-name").value.trim();
    if(!name){showReply("先给工作流起个名字",2000);return;}
    if(!wfState.stages.length){showReply("至少添加一个环节",2000);return;}
    fetch(MAESTRO+"/api/workflows",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:wfEditingId,name:name,definition:{stages:wfState.stages}})})
      .then(function(r){return r.json()})
      .then(function(d){
        if(d&&d.ok){
          wfEditingId=d.id;
          wfState.stages=d.stages;
          renderWfBuilder();
          showReply("工作流已保存 ✔",1800);
          loadWorkflows();
        }else{
          showReply("保存失败："+((d&&d.detail)||"未知错误"),3000);
        }
      })
      .catch(function(){showReply("保存失败：服务未连接",3000)});
  }
  function wfRun(){
    var v=dockInput.value.trim();
    if(!v){showReply("先在下方输入框描述这次要做的事，再点运行",2500);return;}
    var subs=wfCollectSubtasks();
    if(!subs.length){showReply("没有启用的卡片：点卡片点亮至少一张",2500);return;}
    var doRun=function(convId){
      fetch(MAESTRO+"/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({prompt:v,conv_id:convId,subtasks:subs,parallel:true,confirm:false})})
        .then(function(r){return r.json()})
        .then(function(d){
          if(d&&d.task_id){
            winAppendTaskCard(convId,d.task_id,
              d.selected_workers&&d.selected_workers.length?d.selected_workers:["工作流"],null,v);
            resetDockInput();
            // 运行视图：编排器面板切入实时状态（卡片徽章 + 产出预览）
            wfRunViewStart(convId,d.task_id);
          }else{
            showReply("运行失败："+((d&&d.detail)||"未知错误"),3000);
          }
        })
        .catch(function(){showReply("运行失败：服务未连接",3000)});
    };
    if(selectedWinId&&convMeta[selectedWinId]){
      doRun(selectedWinId);
    }else{
      fetch(MAESTRO+"/api/conversations",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({title:document.getElementById("wfb-name").value.trim()||"工作流",kind:"task"})})
        .then(function(r){return r.json()})
        .then(function(d){
          winCreateFromConv(d.conv_id,d.title||"工作流","task");
          selectedWinId=d.conv_id;
          doRun(d.conv_id);
        })
        .catch(function(){showReply("创建会话失败",2500)});
    }
  }
  // 编排器事件绑定（静态元素）
  document.getElementById("wfb-close").addEventListener("click",closeWfBuilder);
  document.getElementById("wfb-save").addEventListener("click",wfSave);
  document.getElementById("wfb-run").addEventListener("click",wfRun);
  document.getElementById("wf-open-builder").addEventListener("click",function(){
    if(!wfAgentsCache.length){
      fetch(MAESTRO+"/api/agents").then(function(r){return r.json()}).then(function(list){
        wfAgentsCache=list||[];wfOpen();
      }).catch(function(){wfOpen();});
    }else{wfOpen();}
  });
  document.getElementById("workflow-select").addEventListener("change",onWorkflowChange);
  loadWorkflows(); // 启动时填充工作流下拉（忘了调用会导致下拉为空）
  document.getElementById("wfb-load").addEventListener("change",function(e){
    var id=e.target.value;
    if(!id)return;
    var wf=wfLibCache.filter(function(w){return w.id===id})[0];
    if(!wf)return;
    wfEditingId=wf.source==="user"?wf.id:null;
    document.getElementById("wfb-name").value=wf.name;
    if(wf.source==="user"){
      // 用户工作流：取详情还原全部环节/卡片
      fetch(MAESTRO+"/api/workflows/"+encodeURIComponent(id)).then(function(r){return r.json()})
        .then(function(d){
          if(d&&d.definition&&d.definition.stages){
            wfState.stages=d.definition.stages.map(function(st){
              return {name:st.name,cards:st.cards.map(function(c){
                return {id:c.id,enabled:c.enabled,desc:c.desc,worker:c.worker,model:c.model,skills:(c.skills||[]).slice(),use_prev:!!c.use_prev};
              })};
            });
            renderWfBuilder();
          }
        });
    }else{
      // 预设 → 单环节模板
      wfState.stages=[{name:wf.name,cards:[{id:"card_"+Date.now(),enabled:true,
        desc:wf.desc||"按预设执行："+wf.name,worker:(wf.workers&&wf.workers[0])||"embedded",
        model:wf.model||null,skills:[]}]}];
      renderWfBuilder();
    }
  });

  // 窗口选择下拉菜单
  var winSelectBtn=document.getElementById("win-select-btn");
  var winSelectVal=document.getElementById("win-select-val");
  var winSelectDropdown=document.getElementById("win-select-dropdown");
  var selectedWinId=null; // 当前选中的窗口 ID
  var pendingNewKind=null; // 待创建窗口的类型（用户点了新建但还没发消息）

  function winSelectRender(){
    var ids=Object.keys(wins);
    var kindLabel=dockMode==="task"?"任务":"聊天";
    var pendingLabel=pendingNewKind?"（待发送）":"";
    var html='<div class="win-select-item" data-action="new">+ 新建 '+kindLabel+' 窗口'+pendingLabel+'</div>';
    if(ids.length>0){
      html+=ids.map(function(k){
        var w=wins[k];
        var isMerged=w.data.kind==="merged";
        var active=(k===selectedWinId||(isMerged&&w.data.children.indexOf(selectedWinId)>=0))?" active":"";
        var label=isMerged?("合并窗口("+w.data.children.length+")"):w.data.title.slice(0,15);
        return '<div class="win-select-item'+active+'" data-id="'+k+'">'+escapeHtml(label)+' ['+(isMerged?"多标签":(w.data.kind==="task"?"任务":"聊天"))+']</div>';
      }).join("");
    }else{
      html+='<div class="win-select-empty">暂无打开的窗口</div>';
    }
    winSelectDropdown.innerHTML=html;
    var selMeta=selectedWinId&&convMeta[selectedWinId];
    if(selMeta)winSelectVal.textContent=selMeta.title.slice(0,8);
    else winSelectVal.textContent="选择窗口";
    // 绑定点击事件
    winSelectDropdown.querySelectorAll(".win-select-item").forEach(function(it){
      it.addEventListener("click",function(){
        if(it.dataset.action==="new"){
          // 标记待创建窗口（发送消息时才真正创建）
          pendingNewKind=dockMode;
          winSelectRender();
        }else{
          var wid=it.dataset.id;
          pendingNewKind=null; // 清除待创建状态
          if(wins[wid]&&wins[wid].data.kind==="merged"){
            winActivate(wid); // 激活合并窗口即选中其当前 tab 会话
          }else{
            selectedWinId=wid;
            updateWinFocus(wid);
            updateDockPlaceholder();
            winSelectRender();
          }
        }
        document.getElementById("win-select").classList.remove("open");
        dockInput.focus();
      });
    });
  }
  // 点击按钮切换下拉
  winSelectBtn.addEventListener("click",function(e){
    e.stopPropagation();
    winSelectRender();
    document.getElementById("win-select").classList.toggle("open");
  });
  // 点击其他地方关闭下拉
  document.addEventListener("click",function(){
    document.getElementById("win-select").classList.remove("open");
  });
  // 窗口创建/关闭时更新下拉
  var _winTabsRender=winTabsRender;
  winTabsRender=function(){
    _winTabsRender();
    winSelectRender();
  };

  function resetDockInput(){dockInput.value="";dockInput.style.height="auto";}
  document.getElementById("merge-btn").addEventListener("click",mergeWindows);
  dockSend.addEventListener("click",dockSendAction);
  // 多行输入：Enter 发送，Shift+Enter 换行；随内容自动增高
  dockInput.addEventListener("keydown",function(e){
    if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();dockSendAction();resetDockInput();}
  });
  dockInput.addEventListener("input",function(){
    dockInput.style.height="auto";
    dockInput.style.height=Math.min(dockInput.scrollHeight,110)+"px";
  });

  // 历史按钮：hover 出会话列表；点击查看当前会话历史消息
  var historyBtn=document.getElementById("history-btn");
  var historyPanel=document.getElementById("history-panel");
  var historyList=document.getElementById("history-list");
  function openHistory(convId,convTitle,retryCount){
    retryCount=retryCount||0;
    historyList.innerHTML='<div class="hp-empty">加载中…</div>';
    document.getElementById("hp-title").textContent=convTitle?("历史对话 · "+convTitle):"历史对话";
    historyPanel.classList.add("show");
    var url=MAESTRO+"/api/chat/history?limit=100"+(convId?("&conv_id="+encodeURIComponent(convId)):"");
    fetch(url,{cache:"no-store"})
      .then(function(r){return r.json()})
      .then(function(d){
        var msgs=d.messages||[];
        if(!msgs.length){historyList.innerHTML='<div class="hp-empty">这个对话还没有消息</div>';return}
        var html="";
        for(var i=0;i<msgs.length;i++){
          var m=msgs[i];
          var role=m.role==="user"?"user":"assistant";
          var t=(m.created_at||"").replace("T"," ").slice(0,16);
          html+='<div class="hp-msg '+role+'"><div>'+escapeHtml(m.content)+'</div><div class="hp-time">'+t+'</div></div>';
        }
        historyList.innerHTML=html;
        historyList.scrollTop=historyList.scrollHeight;
      })
      .catch(function(){
        // 加载失败，显示重试按钮
        if(retryCount<2){
          historyList.innerHTML='<div class="hp-empty" style="text-align:center">加载失败<br><input type="button" id="hp-retry" value="重试" style="margin-top:10px;padding:4px 14px;background:#5b6bb5;color:#fff;border:none;border-radius:4px;cursor:pointer"/></div>';
          var retryBtn=document.getElementById("hp-retry");
          if(retryBtn)retryBtn.onclick=function(){openHistory(convId,convTitle,retryCount+1)};
        }else{
          historyList.innerHTML='<div class="hp-empty">多次加载失败，请检查网络连接</div>';
        }
      });
  }
  historyBtn.addEventListener("click",function(){openHistory(currentConv?currentConv.id:null,currentConv?currentConv.title:"")});
  document.getElementById("history-close").addEventListener("click",function(){historyPanel.classList.remove("show")});
  historyPanel.addEventListener("click",function(e){if(e.target===historyPanel)historyPanel.classList.remove("show")});

  // 历史面板拖拽：按住标题栏拖动（首次拖动时从 transform 居中切换为 left/top 定位）
  var hpDrag={active:false,offX:0,offY:0,inited:false};
  var hpHead=document.querySelector("#history-panel .hp-head");
  hpHead.addEventListener("mousedown",function(e){
    if(e.target.closest(".hp-close"))return; // 关闭按钮不触发拖动
    var rect=historyPanel.getBoundingClientRect();
    if(!hpDrag.inited){
      historyPanel.style.left=rect.left+"px";
      historyPanel.style.top=rect.top+"px";
      historyPanel.style.transform="none";
      hpDrag.inited=true;
    }
    hpDrag.active=true;
    hpDrag.offX=e.clientX-rect.left;
    hpDrag.offY=e.clientY-rect.top;
    e.preventDefault();
  });
  document.addEventListener("mousemove",function(e){
    if(!hpDrag.active)return;
    var r=historyPanel.getBoundingClientRect();
    var x=Math.min(Math.max(e.clientX-hpDrag.offX,-r.width+60),innerWidth-60);
    var y=Math.min(Math.max(e.clientY-hpDrag.offY,0),innerHeight-40);
    historyPanel.style.left=x+"px";
    historyPanel.style.top=y+"px";
  });
  document.addEventListener("mouseup",function(){hpDrag.active=false});
  refreshPlaceholder();
  loadConvs();

  // ========== WebSocket 消息路由 ==========
  // 连接后端 WebSocket，接收消息并根据 conv_id 路由到对应窗口
  (function(){
    var ws=null;
    var reconnectTimer=null;
    var WS_URL=MAESTRO.replace("http","ws")+"/ws";

    function connectWS(){
      try{
        ws=new WebSocket(WS_URL);
        ws.onopen=function(){
          console.log("[WS] 已连接");
          if(reconnectTimer){clearTimeout(reconnectTimer);reconnectTimer=null}
        };
        ws.onmessage=function(event){
          try{
            var msg=JSON.parse(event.data);
            // 消息格式：{conv_id, role, content} 或 {type, conv_id, ...}
            var convId=msg.conv_id;
            if(!convId)return; // 没有 conv_id 的消息忽略

            var role=msg.role||"assistant";
            var content=msg.content||msg.text||"";
            if(!content)return;

            // 根据 conv_id 查找宿主窗口（独立窗口，或合并窗口里的一个 tab）
            var w=winOfConv(convId);
            if(w){
              // 去重：服务端在 SSE 流结束后会把完整回复再广播一遍，而本窗口
              // 已经流式渲染过同一条回复（dataset.text 存原文）。内容一致时跳过，
              // 只在本地流式失败（没有对应气泡）时才真正追加兜底。
              if(role==="assistant"&&content){
                var cont=msgsEl(convId);
                var last=cont&&cont.lastElementChild;
                if(last&&last.classList.contains("assistant")&&last.dataset.text===content){
                  return;
                }
              }
              winAppendMsg(convId,role,content);
              // 合并窗口里非激活 tab：标记未读
              if(w.data.kind==="merged"&&w.activeChild!==convId){
                var tabEl=w.el.querySelector('.mw-tab[data-conv="'+convId+'"]');
                if(tabEl)tabEl.classList.add("unread");
              }
            }else{
              // 窗口不存在，可以选择创建新窗口或忽略
              // 这里选择忽略，也可以通知用户
              console.log("[WS] 收到未知会话的消息，conv_id:",convId);
            }
          }catch(e){
            console.error("[WS] 消息解析失败:",e);
          }
        };
        ws.onclose=function(){
          console.log("[WS] 连接关闭，3秒后重连...");
          reconnectTimer=setTimeout(connectWS,3000);
        };
        ws.onerror=function(e){
          console.error("[WS] 错误:",e);
        };
      }catch(e){
        console.error("[WS] 连接失败:",e);
        reconnectTimer=setTimeout(connectWS,5000);
      }
    }

    // 页面加载后连接 WebSocket
    setTimeout(connectWS,1000);
    // e2e 测试钩子：把 IIFE 内部需要被 Playwright 测试调用的函数暴露到 window
    // （非测试路径无副作用——只多一个对象引用）
    window.__e2e__={
      selectedWinId,updateDockPlaceholder,winAppendTaskCard,renderTaskCard,
      pollTaskCard,showApprovalCard,backfillAllHistories,loadConvs,loadWorkflows,
      mergeWindows,wfOpen,winSelectRender,winCreate,winAppendMsg,
      showTyping,hideTyping,mdRender,newWin,winCreateFromConv,connectEvtWs
    };
  })();
})();
