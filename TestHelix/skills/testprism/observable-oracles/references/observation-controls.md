For performance requirements, observe relevant operations, data movement or resource use when those match the public contract. Avoid arbitrary timing cutoffs. Scope counters, query capture and callbacks around the target action, after setup and before cleanup. Instrumentation must observe rather than replace the target or repair its behavior. Include a supported action showing that observation is actually enabled.

Anchor metamorphic relations with independently justified examples: paired transformations can share the same bug. State why the relation follows from the public contract. Verify final state and cleanup when they are required effects.

For CLI behavior, run a known ordinary input through the same invocation and prove it is processed. An outer assertion can conceal an inner command that read no data. Inspect exit status and the actual artifact or output, not a wrapper's success message.

If alternative encodings or APIs are allowed, preserve that choice through supported discovery and behavioral checks. Do not invent a finite list of acceptable names or require one optional design. Preserve established compatibility without elevating incidental baseline behavior into a new contract. Keep explicit public reproduction inputs.
