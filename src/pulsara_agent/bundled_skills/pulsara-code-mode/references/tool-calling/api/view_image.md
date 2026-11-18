# `tools.view_image(args)`

Read a local image, render a local HTML file, or reread a known image reference. Pass only target: PATH or IMAGE_REF. Image references may come from any conversation. Local HTML rendering produces an image; it does not publish the page. Readable image content is returned in order with its explanation.

```ts
export interface ViewImageArgs {
  target: PathTarget | ImageRefTarget;
}
export interface PathTarget {
  kind: "PATH";
  path: string;
}
export interface ImageRefTarget {
  image_ref: string;
  kind: "IMAGE_REF";
}
export interface ViewImageSuccess {
  target: PathTarget | ImageRefTarget;
  meta: {
    status: "SUCCESS";
  };
  /**
   * @minItems 2
   * @maxItems 2
   */
  content: [
    TextPart,
    {
      type: "image";
      value: ImageRefTarget;
    }
  ];
}
export interface TextPart {
  type: "text";
  value: string;
}
export interface ViewImageFailure {
  target?: PathTarget | ImageRefTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
  };
  content: TextPart[];
}

declare const tools: {
  view_image(args: ViewImageArgs): Promise<ViewImageSuccess>;
};
```
