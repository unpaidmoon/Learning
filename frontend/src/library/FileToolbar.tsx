import { useRef, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";

export type FileMenuEntry =
  | { id: string; label: ReactNode; disabled?: boolean; type?: undefined }
  | { id: string; type: "separator" }
  | { id: string; type: "label"; text: string };
export type FileMenuProps = {
  open: boolean;
  anchor: ReactNode;
  items: FileMenuEntry[];
  selectedIds?: string[];
  onSelect: (id: string) => void;
  onClose: () => void;
  portal: boolean;
  align: "end";
};

/** The desktop supplies its original Menu, keeping one focus and theme implementation. */
export function FileToolbar({
  Menu,
  navigation,
  query,
  onQuery,
  sort,
  view,
  onSort,
  onView,
  disabled,
  loading,
  onAction,
  onFiles,
  selectionActions,
}: {
  Menu: ComponentType<FileMenuProps>;
  navigation: ReactNode;
  query: string;
  onQuery: (value: string) => void;
  sort: string;
  view: "icons" | "list";
  onSort: (value: string) => void;
  onView: (value: "icons" | "list") => void;
  disabled: boolean;
  loading: boolean;
  onAction: (value: string) => void;
  onFiles: (files: File[]) => void;
  selectionActions: FileMenuEntry[];
}) {
  const [menu, setMenu] = useState<"add" | "more" | null>(null);
  const [search, setSearch] = useState(false);
  const searchInput = useRef<HTMLInputElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const add: FileMenuEntry[] = [
    { id: "directory", label: "新建文件夹", disabled },
    { id: "local", label: "添加本地资料…", disabled },
    { id: "paste", label: "粘贴", disabled },
    { id: "bilibili", label: "导入B站链接…", disabled },
  ];
  const more: FileMenuEntry[] = [
    { id: "refresh", label: "刷新", disabled: loading },
    { id: "view-separator", type: "separator" },
    { id: "view-label", type: "label", text: "显示方式" },
    { id: "view-icons", label: "图标视图" },
    { id: "view-list", label: "列表视图" },
    { id: "sort-separator", type: "separator" },
    { id: "sort-label", type: "label", text: "排序方式" },
    ...Object.entries({ name: "名称", type: "类型", size: "大小", modified: "修改时间" }).map(
      ([id, label]) => ({ id: "sort-" + id, label }),
    ),
    ...(selectionActions.length
      ? [{ id: "selection-separator", type: "separator" } as const, ...selectionActions]
      : []),
  ];
  const select = (id: string) => {
    setMenu(null);
    if (id === "local") picker.current?.click();
    else if (id.startsWith("sort-")) onSort(id.slice(5));
    else if (id === "view-icons" || id === "view-list")
      onView(id === "view-icons" ? "icons" : "list");
    else onAction(id);
  };
  return (
    <div className="file-toolbar-compact" role="toolbar" aria-label="文件导航与操作">
      <div className="file-toolbar-navigation">{navigation}</div>
      <div className="file-toolbar-controls">
        {search && (
          <input
            ref={searchInput}
            className="file-manager-search"
            aria-label="搜索当前文件夹"
            autoFocus
            placeholder="搜索当前文件夹"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                onQuery("");
                setSearch(false);
              }
            }}
          />
        )}
        <Button
          variant="ghost"
          size="sm"
          aria-label={search ? "收起文件搜索" : "搜索当前文件夹"}
          title="搜索当前文件夹"
          aria-expanded={search}
          onClick={() => {
            if (search) onQuery("");
            setSearch(!search);
          }}
        >
          <Icon name="search" size={17} />
        </Button>
        <Menu
          open={menu === "add"}
          portal
          align="end"
          items={add}
          onSelect={select}
          onClose={() => setMenu(null)}
          anchor={
            <Button
              variant="ghost"
              size="sm"
              aria-label="添加资料"
              title="添加资料"
              aria-expanded={menu === "add"}
              onClick={() => setMenu(menu === "add" ? null : "add")}
            >
              <Icon name="plus" size={18} />
            </Button>
          }
        />
        <Menu
          open={menu === "more"}
          portal
          align="end"
          items={more}
          onSelect={select}
          onClose={() => setMenu(null)}
          selectedIds={["view-" + view, "sort-" + sort]}
          anchor={
            <Button
              variant="ghost"
              size="sm"
              aria-label="更多文件操作"
              title="更多文件操作"
              aria-expanded={menu === "more"}
              onClick={() => setMenu(menu === "more" ? null : "more")}
            >
              <Icon name="more" size={18} />
            </Button>
          }
        />
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          aria-label="选择本地资料"
          onChange={(e) => {
            const files = Array.from(e.target.files || []);
            e.target.value = "";
            if (files.length) onFiles(files);
          }}
        />
      </div>
    </div>
  );
}
