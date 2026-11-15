(function(){
  if(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  var root=document.documentElement,main=document.querySelector('main')||document.body;
  root.classList.add('js-motion');
  /* reading progress (the handbook already has its own) */
  if(!document.getElementById('prog')){
    var bar=document.createElement('div');bar.className='mprog';bar.setAttribute('aria-hidden','true');document.body.appendChild(bar);
    var raf=0,upd=function(){var h=root.scrollHeight-innerHeight;bar.style.transform='scaleX('+(h>0?Math.min(1,scrollY/h):0)+')';};
    addEventListener('scroll',function(){cancelAnimationFrame(raf);raf=requestAnimationFrame(upd);},{passive:true});upd();
  }
  /* back to top (the handbook already has one in its toolbar) */
  if(!document.getElementById('tools')){
    var t=document.createElement('button');t.type='button';t.className='mtop';t.setAttribute('aria-label','Back to top');t.textContent='↑';
    t.addEventListener('click',function(){scrollTo({top:0,behavior:'smooth'});});document.body.appendChild(t);
    addEventListener('scroll',function(){t.classList.toggle('on',scrollY>900);},{passive:true});
  }
  /* blocks ease in as you reach them */
  var sel='.tw,.pw,.cb,.cards,.call,.cli,figure,.diagram,.note,.gotcha,.unv,.warn:not(.b),.flow,ol.steps,dl.gl,.chk,details.faq,.pager';
  var items=[].slice.call(main.querySelectorAll(sel)).filter(function(e){return !e.parentElement.closest(sel);});
  items.forEach(function(el){
    el.classList.add('mv');
    [].forEach.call(el.querySelectorAll('tbody tr'),function(r,i){r.style.setProperty('--r',Math.min(i,14));});
    if(el.matches('ol.steps'))[].forEach.call(el.children,function(li,i){li.style.setProperty('--r',i);});
    if(el.matches('.flow'))[].forEach.call(el.querySelectorAll('span'),function(s,i){s.style.setProperty('--r',i);});
    [].forEach.call(el.querySelectorAll('svg rect[class]'),function(r,i){r.style.setProperty('--k',Math.min(i,30));});
  });
  if(!('IntersectionObserver' in window)){items.forEach(function(e){e.classList.add('mv-in');});}
  else{
    var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('mv-in');io.unobserve(e.target);}});},{threshold:0.08,rootMargin:'0px 0px -4% 0px'});
    items.forEach(function(e){io.observe(e);});
  }
  /* the contents list follows where you are */
  var side=document.querySelector('.side');
  if(side&&'MutationObserver' in window){
    var mo=new MutationObserver(function(ms){ms.forEach(function(m){var a=m.target;
      if(a.classList&&a.classList.contains('on')&&side.scrollHeight>side.clientHeight){
        var top=a.offsetTop,vis=side.scrollTop;
        if(top<vis+40||top>vis+side.clientHeight-60)side.scrollTo({top:Math.max(0,top-side.clientHeight/3),behavior:'smooth'});}});});
    [].forEach.call(side.querySelectorAll('a'),function(a){mo.observe(a,{attributes:true,attributeFilter:['class']});});
  }
  /* copy buttons confirm */
  document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.copy,.cp');if(!b)return;
    b.classList.remove('done');void b.offsetWidth;b.classList.add('done');setTimeout(function(){b.classList.remove('done');},900);});
})();
