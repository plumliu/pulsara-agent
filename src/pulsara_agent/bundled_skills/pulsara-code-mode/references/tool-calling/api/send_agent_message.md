# `tools.send_agent_message(args)`

Send a message to target:AGENT, with message. An active recipient receives it during its current work; an idle recipient is not started by the message. A recipient without a started work cannot receive it. Unconsumed messages are rejected when the intended recipient turn ends; they are not redirected to a later task.

```ts
export interface SendAgentMessageArgs {
  message: string;
  target: {
    agent_id: string | "ROOT";
    kind: "AGENT";
  };
}
export interface SendAgentMessageSuccess {
  meta: {
    status: "SUCCESS";
  };
  /**
   * @minItems 1
   * @maxItems 1
   */
  content: [
    {
      type: "text";
      value: string;
    }
  ];
}

declare const tools: {
  send_agent_message(args: SendAgentMessageArgs): Promise<SendAgentMessageSuccess>;
};
```
