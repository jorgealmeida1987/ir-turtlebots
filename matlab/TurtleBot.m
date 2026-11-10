classdef TurtleBot < handle
    % TurtleBot - acesso a um TurtleBot4 através do relay (HTTPS).
    %
    %   tb = TurtleBot("robot1", "a12345");                 % liga-se (e passa a controlar o robô)
    %   tb = TurtleBot("robot1", "a12345", "Key", "k7mq");  % chave do robô (dada a cada grupo)
    %
    %   s = tb.scan();          % próximo scan LiDAR: s.ranges, s.angles (como no tcpclient antigo)
    %   o = tb.read("odom");    % qualquer outro tópico do robô (odom, battery_state, imu, ...)
    %   tb.cmd(0.1, 0.0);       % velocidade linear (m/s) e angular (rad/s)
    %   tb.stop();
    %   st = tb.status();       % quem está ligado / quem controla
    %   clear tb                % desliga (o robô para)
    %
    % Regras: quem se liga por último controla o robô. Se não enviares comandos durante ~1 s
    % o robô para sozinho (envia cmd em cada iteração do ciclo de controlo).

    properties (SetAccess = private)
        Robot string
        User string
        BaseUrl string
        Session string = ""
        IsController logical = false
        LastSeq double = -1
    end

    properties (Access = private)
        Key string
        ReadOpts
        WriteOpts
    end

    methods
        function obj = TurtleBot(robot, user, opts)
            arguments
                robot (1,1) string
                user (1,1) string
                opts.BaseUrl (1,1) string = "https://tb.lexicode-consulting.pt"
                opts.Key (1,1) string = ""
                opts.Timeout (1,1) double = 5
            end
            obj.Robot = robot;
            obj.User = user;
            obj.BaseUrl = strip(opts.BaseUrl, "right", "/");
            obj.Key = opts.Key;
            obj.ReadOpts = weboptions("ContentType", "text", "Timeout", opts.Timeout);
            obj.WriteOpts = weboptions("MediaType", "application/json", "ContentType", "text", ...
                                       "Timeout", opts.Timeout);
            try
                obj.connect();
            catch ME
                throwAsCaller(ME);      % erro curto, na linha do aluno
            end
        end

        function connect(obj)
            % Liga-se (outra vez) ao robô e passa a controlá-lo.
            body = struct("user", obj.User);
            if obj.Key ~= "", body.key = obj.Key; end
            try
                r = obj.post("connect", body, false);
            catch ME
                throwAsCaller(ME);
            end
            obj.Session = string(r.session);
            obj.IsController = logical(r.controller);
            obj.LastSeq = -1;
            if ~r.online
                warning("TurtleBot:offline", "%s está offline (ainda não ligado ao relay).", obj.Robot);
            end
        end

        function s = scan(obj)
            % Devolve o próximo scan (diferente do anterior). Espera até ~2 s por um novo.
            try
                s = obj.read("scan", true);
            catch ME
                throwAsCaller(ME);
            end
            s.ranges = s.ranges(:);
            s.angles = s.angles(:);
        end

        function data = read(obj, topic, onlyNew)
            % Última mensagem de um tópico. onlyNew=true espera por uma mensagem nova.
            arguments
                obj
                topic (1,1) string
                onlyNew (1,1) logical = false
            end
            url = obj.BaseUrl + "/api/" + obj.Robot + "/" + topic;
            % obj é um handle: obj.Session/obj.LastSeq são lidos no momento do pedido (também no retry)
            if onlyNew
                fn = @() webread(url, "session", obj.Session, "after", obj.LastSeq, obj.ReadOpts);
            else
                fn = @() webread(url, "session", obj.Session, obj.ReadOpts);
            end
            try
                txt = obj.call(fn, topic, true);
            catch ME
                throwAsCaller(ME);
            end
            data = jsondecode(txt);
            if onlyNew && isfield(data, "seq"), obj.LastSeq = data.seq; end
        end

        function cmd(obj, v, w)
            % Envia velocidade (m/s, rad/s). Erro se outro aluno tiver tomado o controlo.
            try
                obj.post("cmd", struct("session", obj.Session, "v", round(v, 3), "w", round(w, 3)), true);
            catch ME
                throwAsCaller(ME);
            end
        end

        function stop(obj)
            obj.cmd(0, 0);
        end

        function st = status(obj)
            st = jsondecode(webread(obj.BaseUrl + "/api/" + obj.Robot + "/status", obj.ReadOpts));
        end

        function disconnect(obj)
            if obj.Session == "", return; end
            try
                obj.post("disconnect", struct("session", obj.Session), false);
            catch
            end
            obj.Session = "";
        end

        function delete(obj)
            obj.disconnect();
        end
    end

    methods (Access = private)
        function r = post(obj, what, body, retry)
            url = obj.BaseUrl + "/api/" + obj.Robot + "/" + what;
            txt = obj.call(@() webwrite(url, obj.withSession(body), obj.WriteOpts), what, retry);
            r = jsondecode(txt);
        end

        function body = withSession(obj, body)
            if isfield(body, "session"), body.session = obj.Session; end
        end

        function txt = call(obj, fn, what, retry)
            % Executa um pedido. Sessão expirada (401) -> volta a ligar e repete uma vez (se retry).
            % Os outros erros HTTP passam a uma mensagem clara para os alunos.
            try
                txt = fn();
            catch ME
                code = TurtleBot.httpStatus(ME);
                if code == 401 && retry
                    obj.connect();
                    txt = fn();     % fn volta a ler obj.Session através de withSession/args
                    return
                end
                throw(obj.friendlyError(code, what, ME));
            end
        end

        function E = friendlyError(obj, code, what, ME)
            r = obj.Robot;
            switch code
                case 401
                    id = "sessionExpired"; msg = "A sessão expirou: volta a criar o TurtleBot.";
                case 403
                    id = "badKey"; msg = sprintf("Chave errada para o %s: confirma a chave do teu grupo.", r);
                case 404
                    id = "unknownRobot"; msg = sprintf("Não existe nenhum robô '%s' (usa robot1, robot2, ...).", r);
                case 409
                    obj.IsController = false;
                    id = "notController";
                    msg = sprintf("Outro aluno ligou-se ao %s depois de ti e tem agora o controlo. Para voltar a controlar: tb.connect()", r);
                case 423
                    id = "locked"; msg = sprintf("O %s está bloqueado pelo professor.", r);
                case 503
                    id = "offline"; msg = sprintf("O %s está offline (desligado ou sem rede).", r);
                case 504
                    id = "noData"; msg = sprintf("O %s ainda não enviou dados de '%s'.", r, what);
                    if what == "scan"
                        msg = msg + " O LiDAR só roda com o robô fora da base.";
                    end
                otherwise
                    E = ME;
                    return
            end
            E = MException("TurtleBot:" + id, "%s", msg);
        end
    end

    methods (Static, Access = private)
        function code = httpStatus(ME)
            % "The server returned the status 504 with message ..." -> 504 (0 se não for erro HTTP)
            tok = regexp(ME.message, "status (\d{3})", "tokens", "once");
            if isempty(tok), code = 0; else, code = str2double(tok{1}); end
        end
    end
end
