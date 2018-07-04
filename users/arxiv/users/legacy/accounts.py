"""Provide methods for working with user accounts."""

from typing import Optional, Generator, Tuple, Any
import hashlib
from base64 import b64encode, b64decode
from contextlib import contextmanager
from datetime import datetime
import secrets

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session

from arxiv.base import logging
from .. import domain
from . import util, endorsements, exceptions, models
from .models import DBUser, DBUserPassword, DBPermanentToken, \
    DBUserNickname, DBProfile, DBPolicyClass


def username_exists(username: str) -> bool:
    """
    Determine whether a user with a particular username already exists.

    Parameters
    ----------
    username : str

    Returns
    -------
    bool

    """
    with util.transaction() as session:
        data = (
            session.query(DBUserNickname)
            .filter(DBUserNickname.nickname == username)
            .first()
        )
        if data:
            return True
        return False


def email_exists(email: str) -> bool:
    """
    Determine whether a user with a particular address already exists.

    Parameters
    ----------
    email : str

    Returns
    -------
    bool

    """
    with util.transaction() as session:
        data = session.query(DBUser).filter(DBUser.email == email).first()
        if data:
            return True
        return False


def register(user_registration: domain.UserRegistration, ip_address: str,
             remote_host: str) -> Tuple[domain.User, domain.Authorizations]:
    """
    Create a new user from a :class:`.domain.UserRegistration`.

    Parameters
    ----------
    user_registration : :class:`.domain.UserRegistration`
        User registration information.
    ip_address : str
        The IP address of the client requesting the registration.
    remote_host : str
        The remote hostname of the client requesting the registration.

    Returns
    -------
    :class:`.domain.User`
        Data about the created user.
    :class:`.domain.Authorizations`
        Privileges attached to the created user.

    """
    with util.transaction() as session:
        # Main user entry.
        db_user = DBUser(
            first_name=user_registration.name.forename,
            last_name=user_registration.name.surname,
            suffix_name=user_registration.name.suffix,
            email=user_registration.email,
            policy_class=DBPolicyClass.PUBLIC_USER,
            joined_ip_num=ip_address,
            joined_remote_host=remote_host,
            joined_date=util.now(),
            tracking_cookie='',  # TODO: how to set?
        )
        session.add(db_user)

        # Nickname is (apparently) where we keep the username.
        db_nick = DBUserNickname(
            user=db_user,
            nickname=user_registration.username,
            flag_valid=1,
            flag_primary=1
        )
        session.add(db_nick)

        def _has_group(group: str) -> int:
            return int(group in user_registration.profile.submission_groups)

        # TODO: where to put the new categories?
        db_profile = DBProfile(
            user=db_user,
            country=user_registration.profile.country,
            affiliation=user_registration.profile.organization,
            url=user_registration.profile.homepage_url,
            rank=user_registration.profile.rank,
            archive=user_registration.profile.default_archive,
            subject_class=user_registration.profile.default_subject,
            original_subject_classes='',
            flag_group_physics=_has_group('grp_physics'),
            flag_group_math=_has_group('grp_math'),
            flag_group_cs=_has_group('grp_cs'),
            flag_group_q_bio=_has_group('grp_q-bio'),
            flag_group_q_fin=_has_group('grp_q-fin'),
            flag_group_stat=_has_group('grp_stat'),
        )
        session.add(db_profile)

        db_pass = DBUserPassword(
            user=db_user,
            password_enc=util.hash_password(user_registration.password)
        )
        session.add(db_pass)
        session.commit()

        user = domain.User(
            user_id=str(db_user.user_id),
            username=db_nick.nickname,
            email=db_user.email,
            name=domain.UserFullName(
                forename=db_user.first_name,
                surname=db_user.last_name,
                suffix=db_user.suffix_name
            )
        )
        auths = domain.Authorizations(
            classic=util.compute_capabilities(db_user),
            scopes=util.get_scopes(db_user),
            endorsements=endorsements.get_endorsements(user)
        )
        return user, auths


def get_user_by_id(user_id: str) -> domain.User:
    """Load user data from the database."""
    db_user, db_nick, db_profile = _get_user_data(user_id)
    user = domain.User(
        user_id=str(db_user.user_id),
        username=db_nick.nickname,
        email=db_user.email,
        name=domain.UserFullName(
            forename=db_user.first_name,
            surname=db_user.last_name,
            suffix=db_user.suffix_name
        ),
        profile=db_profile.to_domain()
    )
    return user


def update_user(user: domain.User) -> None:
    """Update a user in the database."""
    db_user, db_nick, db_profile = _get_user_data(user.user_id)
    _update_field(db_nick.nickname, user.username)
    _update_field(db_user.email, user.email)
    if user.name is not None:
        _update_field(db_user.first_name, user.name.forename)
        _update_field(db_user.last_name, user.name.surname)
        _update_field(db_user.suffix_name, user.name.suffix)
    if user.profile is not None:
        _update_field(db_profile.origanization, user.profile.organization)
        _update_field(db_profile.country, user.profile.country)
        _update_field(db_profile.rank, user.profile.rank)
        _update_field(db_profile.rank, user.profile.rank)


def _update_field(to_update: Any, update_with: Any) -> None:
    if to_update != update_with:
        to_update = update_with


def _get_user_data(user_id: str) -> Tuple[DBUser, DBUserNickname, DBProfile]:
    with util.transaction() as session:
        db_user, db_nick, db_profile = (
            session.query(DBUser, DBUserNickname, DBProfile)
            .filter(DBUser.user_id == user_id)
            .filter(DBUser.flag_approved == 1)
            .filter(DBUser.flag_deleted == 0)
            .filter(DBUser.flag_banned == 0)
            .filter(DBUserNickname.flag_primary == 1)
            .filter(DBUserNickname.flag_valid == 1)
            .filter(DBUserNickname.user_id == DBUser.user_id)
            .filter(DBProfile.user_id == DBUser.user_id)
            .first()
        )
        if not db_user:
            raise exceptions.NoSuchUser('User does not exist')
    return db_user, db_nick, db_profile
