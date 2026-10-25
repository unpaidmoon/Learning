/** Original handleScroll body; numeric layout port, no React/DOM observers. */
export function retainedHandleScroll(previous:{top:number;height:number;viewport:number},layout:{scrollTop:number;scrollHeight:number;clientHeight:number},following:boolean) {
 const containerRef={current:layout};const positionRef={current:{...previous}};const shouldAutoScrollRef={current:following};
 const rememberPosition=(c:typeof layout)=>{positionRef.current={top:c.scrollTop,height:c.scrollHeight,viewport:c.clientHeight};};
 const handle=() => {
    const container = containerRef.current;
    if (!container) return;
    const previous = positionRef.current;
    const sameLayout = previous.height === container.scrollHeight &&
      previous.viewport === container.clientHeight;
    // The browser clamps scrollTop when content shrinks. That is not a user
    // gesture. Neither clamping nor delayed growth may release/re-arm follow.
    if (sameLayout) {
      const remaining = container.scrollHeight - container.scrollTop - container.clientHeight;
      if (remaining < 80 && (shouldAutoScrollRef.current || container.scrollTop > previous.top + 4)) {
        shouldAutoScrollRef.current = true;
      }
      else if (container.scrollTop < previous.top - 4) shouldAutoScrollRef.current = false;
    }
    rememberPosition(container);
  };handle();return {following:shouldAutoScrollRef.current,remembered:positionRef.current};
}
