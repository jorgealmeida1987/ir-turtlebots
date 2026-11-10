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
            obj.connect();
        end

        function connect(obj)
            body = struct("user", obj.User);
            if obj.Key ~= "", body.key = obj.Key; end
            r = obj.post("connect", body, false);
            obj.Session = string(r.session);
            obj.IsController = logical(r.controller);
            obj.LastSeq = -1;
            if ~r.online
                warning("TurtleBot:offline", "%s está offline (ainda não ligado ao relay).", obj.Robot);
            end
        end

        function s = scan(obj)
            % Devolve o próximo scan (diferente do anterior). Espera até ~2 s por um novo.
            s = obj.read("scan", true);
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
            txt = obj.call(fn);
            data = jsondecode(txt);
            if onlyNew && isfield(data, "seq"), obj.LastSeq = data.seq; end
        end

        function cmd(obj, v, w)
            % Envia velocidade (m/s, rad/s). Erro se outro aluno tiver tomado o controlo.
            obj.post("cmd", struct("session", obj.Session, "v", round(v, 3), "w", round(w, 3)), true);
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
            if retry
                txt = obj.call(@() webwrite(url, obj.withSession(body), obj.WriteOpts));
            else
                txt = webwrite(url, body, obj.WriteOpts);
            end
            r = jsondecode(txt);
        end

        function body = withSession(obj, body)
            if isfield(body, "session"), body.session = obj.Session; end
        end

        function txt = call(obj, fn)
            % Executa um pedido; se a sessão expirou (401) volta a ligar e repete uma vez.
            try
                txt = fn();
            catch ME
                if contains(ME.message, "401")
                    obj.connect();
                    txt = fn();     % fn volta a ler obj.Session através de withSession/args
                elseif contains(ME.message, "409")
                    obj.IsController = false;
                    error("TurtleBot:notController", ...
                        "Outro aluno ligou-se a %s depois de ti e tem agora o controlo.", obj.Robot);
                elseif contains(ME.message, "503")
                    error("TurtleBot:offline", "%s está offline.", obj.Robot);
                else
                    rethrow(ME);
                end
            end
        end
    end
end
