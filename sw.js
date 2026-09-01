/* Сервис-воркер «Домашнего реестра»: принимает пуши, когда приложение закрыто. */
var ICON='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="18" fill="%2333684E"/><text x="50" y="70" font-size="58" text-anchor="middle">📦</text></svg>';

self.addEventListener('install',function(e){self.skipWaiting();});
self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim());});

self.addEventListener('push',function(event){
  var data={title:'Домашний реестр',body:'Есть дела на сегодня',url:''};
  try{ if(event.data) data=Object.assign(data,event.data.json()); }
  catch(e){ if(event.data) data.body=event.data.text(); }
  event.waitUntil(self.registration.showNotification(data.title,{
    body:data.body,
    icon:ICON,
    badge:ICON,
    tag:'home-registry-daily',
    renotify:true,
    data:{url:data.url||'./'}
  }));
});

self.addEventListener('notificationclick',function(event){
  event.notification.close();
  var target=(event.notification.data&&event.notification.data.url)||'./';
  event.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(function(list){
    for(var i=0;i<list.length;i++){
      if('focus' in list[i])return list[i].focus();
    }
    if(self.clients.openWindow)return self.clients.openWindow(target);
  }));
});
