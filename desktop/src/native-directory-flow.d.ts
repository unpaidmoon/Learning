declare module "@learning/native-directory-flow" {
  import type { ComponentType } from "react";

  export const NativeDirectoryFlow: ComponentType<{
    open: boolean;
    busy: boolean;
    pick: () => Promise<string | null>;
    onPicked: (path: string) => void;
    onCancel: () => void;
    onError: (message: string) => void;
  }>;
}
