async (page) => {
page.__offlineProbe = {requests:[], failedRequests:[], webSockets:[], pageErrors:[], consoleErrors:[], dialogs:[]};
await page.context().setOffline(true);
await page.context().route(/^https?:\/\//, async route => {
  page.__offlineProbe.failedRequests.push({url:route.request().url(), reason:"blocked_by_offline_harness"});
  await route.abort();
});
page.on("request", request => page.__offlineProbe.requests.push({url:request.url(),resourceType:request.resourceType()}));
page.on("requestfailed", request => page.__offlineProbe.failedRequests.push({url:request.url(),reason:request.failure()?.errorText}));
page.on("websocket", socket => page.__offlineProbe.webSockets.push(socket.url()));
page.on("pageerror", error => page.__offlineProbe.pageErrors.push(String(error)));
page.on("console", message => {if(message.type()==="error") page.__offlineProbe.consoleErrors.push(message.text());});
page.on("dialog", async dialog => {page.__offlineProbe.dialogs.push(dialog.message());await dialog.dismiss();});
await page.context().addInitScript(() => {
  window.__offlineStorageWrites=[];
  for(const name of ["setItem","removeItem","clear"]) {
    const original=Storage.prototype[name];
    Storage.prototype[name]=function(...args){window.__offlineStorageWrites.push({api:`Storage.${name}`,key:String(args[0]??"")});return Reflect.apply(original,this,args);};
  }
  if(globalThis.IDBFactory) {
    const original=IDBFactory.prototype.open;
    IDBFactory.prototype.open=function(...args){window.__offlineStorageWrites.push({api:"indexedDB.open",name:String(args[0])});return Reflect.apply(original,this,args);};
  }
});
console.log(JSON.stringify({offlineObservationInstalled:true,browser:page.context().browser()?.version()??"unknown"}));
}
