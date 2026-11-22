// Ego's taskSpace(name) opens a new space and taskSpace(id) resumes one (ego-browser skill).
const space = (await listTaskSpaces()).find(s => s.name === INPUT.space && s.ownership === "agent");
if (space) await (await taskSpace(space.id)).finish({ keep: [] });
