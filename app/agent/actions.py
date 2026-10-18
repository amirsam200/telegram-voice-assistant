"""The validated actions the agent may propose. Nothing runs until the user confirms."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.agent.fa import has_persian, parse_date, parse_time


class NewContact(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr


class ScheduleAction(BaseModel):
    type: Literal["schedule"] = "schedule"
    date: dt.date = Field(description="Event date, Gregorian YYYY-MM-DD.")
    time: dt.time = Field(description="Start time, 24h HH:MM, Asia/Tehran.")
    duration_min: int = Field(ge=5, le=720, description="Length in minutes.")
    title: str = Field(min_length=1, max_length=300, description="Short title in Persian.")
    description: str = Field(default="", max_length=4000, description="Description in Persian.")
    guests: list[EmailStr] = Field(
        default_factory=list, max_length=20, description="Guest emails, only from contacts or the user's words."
    )
    new_contacts: list[NewContact] = Field(
        default_factory=list,
        max_length=5,
        description="People the user named WITH an email in this request who are not saved contacts yet.",
    )

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v):
        return parse_date(v) if isinstance(v, str) else v

    @field_validator("time", mode="before")
    @classmethod
    def _time(cls, v):
        return parse_time(v) if isinstance(v, str) else v

    @field_validator("title")
    @classmethod
    def _persian_title(cls, v: str) -> str:
        if not has_persian(v):
            raise ValueError("title must be written in Persian")
        return v.strip()

    def emails(self) -> list[str]:
        return [str(e) for e in self.guests] + [str(c.email) for c in self.new_contacts]


class EmailAction(BaseModel):
    type: Literal["email"] = "email"
    to: EmailStr = Field(description="One recipient email, only from contacts or the user's words.")
    subject: str = Field(min_length=1, max_length=300, description="Subject in Persian.")
    body: str = Field(min_length=1, max_length=10000, description="Body in Persian.")
    new_contacts: list[NewContact] = Field(
        default_factory=list,
        max_length=5,
        description="People the user named WITH an email in this request who are not saved contacts yet.",
    )

    @field_validator("subject", "body")
    @classmethod
    def _persian(cls, v: str) -> str:
        if not has_persian(v):
            raise ValueError("subject and body must be written in Persian")
        return v.strip()

    def emails(self) -> list[str]:
        return [str(self.to)] + [str(c.email) for c in self.new_contacts]


class AddContactAction(BaseModel):
    type: Literal["add_contact"] = "add_contact"
    name: str = Field(min_length=1, max_length=200, description="The contact's name as the user said it.")
    email: EmailStr = Field(description="The address exactly as the user spelled it.")
    # Filled in by the app (not the model) when a contact with this name already exists.
    previous_email: SkipJsonSchema[str | None] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return str(v).lower()

    @property
    def new_contacts(self) -> list[NewContact]:
        return []

    def emails(self) -> list[str]:
        return [str(self.email)]


class DeleteContactAction(BaseModel):
    """Only created from the /delcontact command, never by the model."""

    type: Literal["delete_contact"] = "delete_contact"
    contact_id: int
    name: str
    email: str | None = None

    @property
    def new_contacts(self) -> list[NewContact]:
        return []

    def emails(self) -> list[str]:
        return []


class Clarification(BaseModel):
    question: str = Field(min_length=1, max_length=1000, description="One short question in Persian.")

    @field_validator("question")
    @classmethod
    def _persian(cls, v: str) -> str:
        if not has_persian(v):
            raise ValueError("the question must be written in Persian")
        return v.strip()


ContactAction = AddContactAction | DeleteContactAction
Action = ScheduleAction | EmailAction | AddContactAction | DeleteContactAction


def intent_of(action: Action) -> str:
    """The agent_runs.intent value for an action."""
    return "contact" if isinstance(action, (AddContactAction, DeleteContactAction)) else action.type
